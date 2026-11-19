#!/usr/bin/env python3
"""台帳の掲載候補(active)の全件到達確認(Issue #37 第2段・投入前の必須工程)。

2 出典のホスト一致は「今も生きている」保証にならない(閉鎖・移転・ドメイン失効)。投入前に各トップへ 1 回だけアクセスする。
  uv run python3 scripts/public_bodies_reach.py fetch   # 未確認の分だけ取得し evidence/reachability.csv に追記(再開できる。約1900件で1時間前後)
  uv run python3 scripts/public_bodies_reach.py apply   # evidence を読んで台帳へ反映(ネットワークなし・冪等)

作法: robots.txt を確認(禁止なら取得しない)、UA は MiseResearchBot、1 ホスト 1 リクエスト(+robots)、直列、約1.5秒間隔、
リダイレクトは最大5回追って最終 URL を記録、タイムアウト30秒。
判定(verdict): 200 系 かつ 最終ホストが公的(.lg.jp/.go.jp、city./town./vill./pref. 型、または台帳ホストと同一サイト) -> ok。
  ok 以外は掲載しない。dead(名前解決不可・404/410・5xx)は review.csv の unreachable へ戻す。
  unverified(タイムアウト・接続失敗・403/401・robots 禁止。本環境からの接続制限の可能性があり、閉鎖とは言えない)は
  台帳の status を unverified-reach にして掲載しない(安全側)。other-org(別組織への転送)・parked(売却/パーキング表示)は review へ。
ok で最終 URL の scheme・ホストが台帳 URL と違えば、scheme とホストだけを最終 URL のものに置換し(http→https、旧ホスト→新ホスト。パスは台帳のまま)、ホストが変わったときは note に旧→新を残す。
"""
import csv, datetime, pathlib, re, subprocess, sys, tempfile, time, urllib.parse
from public_bodies_common import host_key, clean_url, url_key
import public_bodies_fetch as F

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies'
EVID = ROOT / 'evidence' / 'reachability.csv'
FIELDS = ['code', 'from_url', 'robots', 'http_status', 'curl_exit', 'final_url', 'title', 'verdict', 'checked_at']
LISTED = ('都道府県', '市', '町', '村', '特別区', '国の機関', '独立行政法人')
PARK = re.compile(r'domain (is )?for sale|this domain|ドメイン(は|が).{0,12}(販売|売却|失効|停止)|購入|parked|under construction', re.I)
PUBLIC_HOST = re.compile(r'(^|\.)(lg|go)\.jp$|^(www\.)?(city|town|vill|village|pref|metro)\.[a-z0-9-]+\.[a-z0-9.-]*jp$', re.I)
CYCLE = 0.75  # robots・本体それぞれの後の待機(秒)。合計で約1.5秒間隔

_ROBOTS = {}
def allowed(url):
    """robots.txt をホストごとに1回取得して確認。200 以外・HTML が返るものは規則なし扱い。取得自体が失敗したホストは本体も取得できない可能性が高いが、判定は本体の結果に任せる。"""
    import urllib.robotparser
    sp = urllib.parse.urlsplit(url); host = f'{sp.scheme}://{sp.netloc}'
    if host not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        r = subprocess.run(['curl', '--silent', '--connect-timeout', '15', '--max-time', '20', '--max-filesize', '500000', '--user-agent', F.UA,
                            '--write-out', '\n%{http_code}', host + '/robots.txt'], capture_output=True)
        blob = r.stdout
        code = int((blob.rsplit(b'\n', 1)[1] or b'0')) if b'\n' in blob else 0
        body = blob.rsplit(b'\n', 1)[0]
        if code == 200 and not body.lstrip().startswith(b'<'): rp.parse(body.decode('utf-8', errors='replace').splitlines())
        else: rp.parse([])
        _ROBOTS[host] = rp; time.sleep(CYCLE)
    return _ROBOTS[host].can_fetch(F.UA, url)

def same_site(final, ledger_host):
    f = host_key(final)
    if f == ledger_host: return True
    # 同じサイトの別サブドメイン(例 www2.city.x.jp)
    return f.split('.', 1)[-1] == ledger_host.split('.', 1)[-1] and f.count('.') >= 3

def public_host(final, ledger_host):
    return bool(PUBLIC_HOST.search(host_key(final))) or same_site(final, ledger_host)

def fetch_one(url):
    with tempfile.NamedTemporaryFile() as tf:
        cmd = ['curl', '--silent', '--location', '--max-redirs', '5', '--connect-timeout', '15', '--max-time', '30', '--max-filesize', '3000000',
               '--user-agent', F.UA, '--output', tf.name, '--write-out', '%{http_code} %{url_effective}', url]
        r = subprocess.run(cmd, capture_output=True)
        out = r.stdout.decode(errors='replace').strip()
        code, _, final = out.partition(' ')
        body = open(tf.name, 'rb').read(300000).decode('utf-8', errors='replace')
    m = re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S)
    title = re.sub(r'\s+', ' ', m.group(1)).strip()[:80] if m else ''
    return r.returncode, int(code or 0), final, title

def verdict(row, robots_ok, exit_code, status, final, title):
    if not robots_ok: return 'unverified'
    if exit_code in (6,): return 'dead'                       # 名前解決不可
    if exit_code not in (0, 63): return 'unverified'          # 接続失敗・TLS・タイムアウト・リセット
    if status in (404, 410) or status >= 500: return 'dead'
    if status in (401, 403, 429) or not (200 <= status < 300): return 'unverified'
    if PARK.search(title or ''): return 'parked'
    if not public_host(final, row['host']): return 'other-org'
    return 'ok'

def load_ledger():
    return list(csv.DictReader(open(ROOT / 'ledger.csv', encoding='utf-8')))

def load_evid():
    return list(csv.DictReader(open(EVID, encoding='utf-8'))) if EVID.exists() else []

def write_evid(rows):
    EVID.parent.mkdir(exist_ok=True)
    with open(EVID, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n'); w.writeheader()
        for r in sorted(rows, key=lambda r: r['code']): w.writerow(r)

def do_fetch():
    evid = {e['code']: e for e in load_evid()}
    rows = [r for r in load_ledger() if r['status'] == 'active' and r['type'] in LISTED]
    todo = [r for r in rows if not (r['code'] in evid and r['url'] in (evid[r['code']]['from_url'], evid[r['code']]['final_url']))]
    print(f'対象 {len(rows)} / 未確認 {len(todo)}', file=sys.stderr)
    for i, r in enumerate(todo):
        ok = allowed(r['url'])
        if ok: ec, st, final, title = fetch_one(r['url']); time.sleep(CYCLE)
        else: ec, st, final, title = -1, 0, '', ''
        evid[r['code']] = {'code': r['code'], 'from_url': r['url'], 'robots': 'allowed' if ok else 'disallowed', 'http_status': st, 'curl_exit': ec,
                           'final_url': final, 'title': title, 'verdict': verdict(r, ok, ec, st, final, title),
                           'checked_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        if i % 25 == 0 or i == len(todo) - 1:
            write_evid(list(evid.values())); print(i + 1, '/', len(todo), r['name_ja'], evid[r['code']]['verdict'], file=sys.stderr, flush=True)
    write_evid(list(evid.values()))

def do_apply():
    import public_bodies_build as B
    evid = {e['code']: e for e in load_evid()}
    ledger, review = load_ledger(), list(csv.DictReader(open(ROOT / 'review.csv', encoding='utf-8')))
    tally = {}
    for r in ledger:
        e = evid.get(r['code'])
        if r['status'] not in ('active', 'unverified-reach') or r['type'] not in LISTED or not e: continue
        if r['url'] not in (e['from_url'], e['final_url']): continue  # 台帳側が変わった(再 build 後など)。再確認が要る
        v = e['verdict']; tally[v] = tally.get(v, 0) + 1
        day = e['checked_at'][:10]
        if v == 'ok':
            r['status'] = 'active'
            # 置換するのは scheme とホストだけ。パスは台帳のまま(トップが /cms/ や /top/ や保守ページへ飛ぶ場合に、その先を掲載URLにしないため)
            fu, ou = urllib.parse.urlsplit(e['final_url']), urllib.parse.urlsplit(r['url'])
            new = clean_url(f'{fu.scheme}://{fu.netloc}{ou.path}')
            if url_key(new) != url_key(r['url']) or new.split(':')[0] != r['url'].split(':')[0]:
                if url_key(new) != url_key(r['url']):
                    r['note'] = (r['note'] + ' ' if r['note'] else '') + f'URL移転: {r["url"]} -> {new} (到達確認 {day})'
                r['url'], r['host'], r['domain_class'] = new, host_key(new), B.domain_class(host_key(new))
            if 'reach@' not in r['sources']: r['sources'] += f';reach@{day}'
        elif v == 'unverified':
            r['status'] = 'unverified-reach'
            why = 'robots禁止' if e['robots'] == 'disallowed' else 'curl=%s http=%s' % (e['curl_exit'], e['http_status'])
            msg = f'到達未確認({why}、{day})。既定は掲載しない'
            if msg not in r['note']: r['note'] = (r['note'] + ' ' if r['note'] else '') + msg
        else:  # dead / parked / other-org -> 要確認へ戻す
            review.append({'code': r['code'], 'name_ja': r['name_ja'], 'type': r['type'], 'prefecture': r['prefecture'], 'reason': f'unreachable:{v}',
                           'wikidata_urls': '', 'egov_url': r['url'], 'jlis_url': '', 'domain_class': r['domain_class'], 'wikidata_name': '',
                           'note': f'到達確認 {day}: http={e["http_status"]} curl={e["curl_exit"]} 最終={e["final_url"]}'})
            r['status'] = 'drop'
    ledger = [r for r in ledger if r['status'] != 'drop']
    for name, rows, fields in (('ledger.csv', ledger, B.FIELDS), ('review.csv', review, B.REVIEW_FIELDS)):
        with open(ROOT / name, 'w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator='\n'); w.writeheader()
            for r in sorted(rows, key=lambda r: r['code']): w.writerow(r)
    print(tally, f'ledger {len(ledger)} / review {len(review)}')

if __name__ == '__main__':
    {'fetch': do_fetch, 'apply': do_apply}[sys.argv[1] if len(sys.argv) > 1 else 'apply']()
