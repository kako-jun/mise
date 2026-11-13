"""登録済みサイトの再巡回(手元実行)。決定論のみ、LLM なし、本番 D1 には書かない。

台帳(SQLite, 既定 var/recrawl.sqlite)に結果と状態を持ち、`candidates` で人が見る一覧、`sql` で
site_checks(migrations/0017_recrawl.sql)向けの SQL を出す。自動削除・紹介文の書き換えはしない。
取得作法は scripts/pipeline/fetcher.py を再利用(UA=MiseResearchBot、robots 遵守、内部IP拒否)。
"""
import argparse
import functools
import concurrent.futures
import datetime
import glob
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import fetcher, hostnames  # noqa: E402

DEFAULT_DB = ROOT / 'var' / 'recrawl.sqlite'
HOST_WAIT = 5.0              # 同一ホストへのリクエスト間隔(秒)。発見パイプラインより長め
OK_INTERVAL_DAYS = 30        # 正常サイトの再確認間隔
MAX_BACKOFF_DAYS = 7         # 失敗中の再確認間隔の上限(1,2,4,7日)
REVIEW_AFTER = 3             # 連続失敗がこの回数以上で 要確認
HIDE_AFTER = 6               # 連続失敗がこの回数以上かつ
HIDE_MIN_DAYS = 14           # 最初の失敗からこの日数以上で 非表示候補
HASH_TEXT_CHARS = 20000

# 失敗として数える種別 / 数えない(サイトの生死を判断できない)種別
FAIL_KINDS = {'http_404', 'http_410', 'http_4xx', 'http_5xx', 'dns', 'timeout', 'connect', 'tls', 'too_many_redirects'}
NEUTRAL_KINDS = {'robots_disallowed', 'http_denied', 'error'}
FLAG_KINDS = {'redirect_offsite': '別ドメインへ転送(移転・乗っ取り・売却の疑い)', 'ssrf_blocked': '内部アドレスへ解決(失効後の乗っ取り・設定ミスの疑い)'}
NETWORK_KINDS = {'dns', 'timeout', 'connect', 'tls', 'ssrf_blocked'}   # ssrf_blocked: キャプティブポータル/DNS ハイジャックで全件に出うる
# 実行機のネットワーク障害の検出用。全件失敗の回を失敗に数えないため、先にここへ到達できるか確かめる
NETWORK_FAIL_RATIO = 0.5
HEALTH_URLS = ('https://www.w3.org/', 'https://www.mext.go.jp/')
# 共有ホスティング(サブドメインごとに別の持ち主)。ここに属するホストは完全一致で同一サイトとみなす
EXTRA_SHARED = {'github.io', 'blogspot.com', 'netlify.app', 'pages.dev', 'workers.dev', 'vercel.app', 'wordpress.com', 'herokuapp.com',
                'web.app', 'firebaseapp.com', 'appspot.com', 'tumblr.com', 'wixsite.com', 'weebly.com', 'gitlab.io', 'sakura.ne.jp', 'lolipop.jp'}
# ok / redirect_offsite はそれぞれ成功 / 要確認(転送先が別ドメイン=乗っ取り・移転の疑い)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  site_id TEXT PRIMARY KEY, url TEXT NOT NULL, name TEXT,
  state TEXT NOT NULL DEFAULT 'listed',            -- listed | needs_review | hide_candidate
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  flag TEXT,                                       -- 人の確認まで needs_review を保つ理由(offsite | ssrf)
  first_failure_at TEXT, last_checked_at TEXT, last_ok_at TEXT,
  last_kind TEXT, last_http_status INTEGER, last_error TEXT, last_final_url TEXT,
  title TEXT, content_hash TEXT, next_check_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, checked_at TEXT NOT NULL, kind TEXT NOT NULL,
  http_status INTEGER, final_url TEXT, error TEXT, state_after TEXT);
CREATE TABLE IF NOT EXISTS confirmations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, confirmed_at TEXT NOT NULL, flag TEXT, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, detected_at TEXT NOT NULL, field TEXT NOT NULL, old TEXT, new TEXT);
"""


def now_iso(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path):
    path = Path(path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path)); db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def import_sites(db, files):
    """掲載済みサイトの JSON(id, url, name)を取り込む。既存は url/name だけ更新し状態は保つ。"""
    n = 0
    for f in files:
        for row in json.loads(Path(f).read_text(encoding='utf-8')):
            if not row.get('id') or not row.get('url'):
                continue
            db.execute("INSERT INTO sites(site_id,url,name) VALUES(?,?,?) ON CONFLICT(site_id) DO UPDATE SET url=excluded.url,name=excluded.name",
                       (row['id'], row['url'], row.get('name')))
            n += 1
    db.commit()
    return n


# ---- 取得と分類 ----

def _strip(h):
    return h[4:] if h.startswith('www.') else h


@functools.lru_cache(maxsize=1)
def _shared_suffixes():
    out = set(EXTRA_SHARED)
    for entries in hostnames.blocklist('platforms.txt').values():
        out.update(entries)
    return frozenset(out)


def _site_key(host):
    """同一サイト判定のキー。共有ホスティング配下は完全なホスト名、それ以外は登録ドメイン。"""
    h = _strip(host.lower())
    for suf in _shared_suffixes():
        if h == suf or h.endswith('.' + suf):
            return h
    return hostnames.registrable(h)


def _same_domain(a, b):
    return _site_key(a) == _site_key(b)


def classify_error(e):
    if e.code == 'connect':
        d = e.detail.lower()
        if 'timed out' in d:
            return 'timeout'
        if 'ssl' in d or 'certificate' in d or 'tls' in d:
            return 'tls'
        if 'could not resolve' in d:
            return 'dns'
        return 'connect'
    if e.code in ('dns', 'ssrf_blocked'):
        return e.code
    return 'error'


def classify_status(status):
    if 200 <= status < 300:
        return 'ok'
    if status in (404, 410):
        return f'http_{status}'
    if status in (401, 403, 429):
        return 'http_denied'
    if 400 <= status < 500:
        return 'http_4xx'
    if status >= 500:
        return 'http_5xx'
    return 'error'


def extract_title(html):
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    return re.sub(r'\s+', ' ', m.group(1)).strip()[:300] if m else ''


def text_hash(html):
    t = re.sub(r'(?is)<(script|style|noscript)\b.*?</\1>', ' ', html)
    t = re.sub(r'(?s)<!--.*?-->', ' ', t)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()[:HASH_TEXT_CHARS]
    return hashlib.sha256(t.encode('utf-8')).hexdigest()[:16]


class Robots(fetcher.RobotsCache):
    """robots.txt の取得。RFC 9309 に従い、5xx・接続失敗は全面 disallow 扱い(本体は取りに行かない)。
    その場合は ('error', (種別, HTTP状態)) を返し、呼び出し側が robots.txt 取得自体の失敗として分類する。
    別ドメインへの転送は ('offsite', 転送先)。"""
    def _fetch(self, origin):
        url = origin + '/robots.txt'
        host = urllib.parse.urlsplit(origin).hostname
        try:
            for _ in range(fetcher.MAX_REDIRECTS + 1):
                self.sleep(host)
                status, headers, body = fetcher.http_get(url, fetcher.MAX_ROBOTS)
                if 300 <= status < 400 and headers.get('location'):
                    nxt = urllib.parse.urljoin(url, headers['location'])
                    nh = urllib.parse.urlsplit(nxt).hostname or ''
                    if not _same_domain(nh, host):
                        return 'offsite', nxt
                    url = nxt; host = nh; continue
                break
        except fetcher.FetchError as e:
            return 'error', (classify_error(e), None, e.detail[:200])
        if status == 200:
            p = urllib.robotparser.RobotFileParser(); p.parse(fetcher.decode_body(body, headers).splitlines())
            return 'allowed', p
        if status in (401, 403):
            return 'disallowed', None
        if status == 429 or status >= 500 or status < 400 and status >= 300:
            return 'error', (classify_status(status) if status != 429 else 'http_denied', status, f'robots.txt HTTP {status}')
        return 'allowed_no_robots', None      # 404/410 ほか 4xx は規則なし

    def check(self, origin, path):
        if origin not in self.rules:
            self.rules[origin] = self._fetch(origin)
        st, p = self.rules[origin]
        if st in ('offsite', 'error'):
            return st, False, p
        return super().check(origin, path)


def check_url(url, robots, sleep=time.sleep, host_wait=HOST_WAIT):
    """1サイトのトップを robots 確認の上で取得し分類する。戻り dict: kind, http_status, final_url, error, title, hash

    robots.txt が 5xx・タイムアウト・接続失敗のときは本体を取らず(RFC 9309: 全面 disallow)、robots.txt 取得自体の
    結果(http_5xx / timeout / connect など)を失敗として返す。robots.txt が別ドメインへ転送されたら redirect_offsite。"""
    out = {'kind': 'error', 'http_status': None, 'final_url': url, 'error': None, 'title': None, 'hash': None}
    first_host = urllib.parse.urlsplit(url).hostname or ''
    cur = url
    for _ in range(fetcher.MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(cur)
        origin = f'{parts.scheme}://{parts.netloc}'
        st, ok, delay = robots.check(origin, parts.path or '/')
        if st == 'offsite':
            out.update(kind='redirect_offsite', final_url=delay); return out
        if st == 'error':
            kind, status, detail = delay
            out.update(kind=kind, http_status=status, error=detail, final_url=origin + '/robots.txt'); return out
        if not ok:
            out['kind'] = 'robots_disallowed'; return out
        sleep(max(host_wait, delay))
        try:
            status, headers, body = fetcher.http_get(cur)
        except fetcher.FetchError as e:
            out.update(kind=classify_error(e), error=e.detail[:200], final_url=cur); return out
        out.update(http_status=status, final_url=cur)
        if 300 <= status < 400 and headers.get('location'):
            nxt = urllib.parse.urljoin(cur, headers['location'])
            np_ = urllib.parse.urlsplit(nxt)
            if np_.scheme not in ('http', 'https') or not np_.hostname:
                out['kind'] = 'error'; out['error'] = 'bad redirect'; return out
            if not _same_domain(np_.hostname, first_host):
                out.update(kind='redirect_offsite', final_url=nxt); return out
            cur = nxt; out['final_url'] = nxt; continue
        out['kind'] = classify_status(status)
        if out['kind'] == 'ok':
            html = fetcher.decode_body(body, headers)
            out['title'] = extract_title(html); out['hash'] = text_hash(html)
        return out
    out['kind'] = 'too_many_redirects'
    return out


# ---- 状態遷移・優先度 ----

def parse_iso(s):
    return datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)


def next_state(kind, failures, first_failure_at, now, flag=None, state='listed'):
    """(連続失敗回数, 状態, 理由, flag) を返す。自動削除はない。

    flag(offsite/ssrf)が付いた状態は、成功しても人が `confirm` するまで維持する。"""
    if kind == 'ok':
        if flag:       # 転送後に売却ページが 200 で返る等があるため、人が confirm するまで維持
            return 0, state, None, flag
        return 0, 'listed', None, None
    if kind in FLAG_KINDS:
        return failures, 'needs_review', FLAG_KINDS[kind], 'offsite' if kind == 'redirect_offsite' else 'ssrf'
    if kind in NEUTRAL_KINDS:
        return failures, None, None, flag
    failures += 1
    if failures >= HIDE_AFTER and first_failure_at and (now - parse_iso(first_failure_at)).days >= HIDE_MIN_DAYS:
        return failures, 'hide_candidate', f'連続失敗{failures}回・{HIDE_MIN_DAYS}日以上', flag
    if failures >= REVIEW_AFTER or flag:
        return failures, 'needs_review', (None if flag else f'連続失敗{failures}回'), flag
    return failures, 'listed', None, flag


def next_check(kind, failures, now):
    if kind == 'ok':
        days = OK_INTERVAL_DAYS
    elif kind in NEUTRAL_KINDS:
        days = 7
    elif kind in FLAG_KINDS:
        days = 3
    else:
        days = min(2 ** max(failures - 1, 0), MAX_BACKOFF_DAYS)
    return now_iso(now + datetime.timedelta(days=days))


def due_sites(db, now, limit=None):
    """期限が来たサイトを優先度順に返す: 失敗中(連続失敗が多い順) → 最終確認が古い順(未確認が先頭)。"""
    rows = db.execute("SELECT * FROM sites WHERE next_check_at IS NULL OR next_check_at <= ? "
                      "ORDER BY (consecutive_failures>0) DESC, consecutive_failures DESC, last_checked_at IS NOT NULL, last_checked_at ASC, site_id",
                      (now_iso(now),)).fetchall()
    return rows[:limit] if limit else rows


def apply_result(db, site, res, now):
    """結果を台帳に記録する(状態遷移・変更検出)。"""
    ts = now_iso(now); kind = res['kind']
    failures, state, reason, flag = next_state(kind, site['consecutive_failures'], site['first_failure_at'], now, site['flag'], site['state'])
    state = state or site['state']
    first_fail = site['first_failure_at']
    if kind == 'ok':
        first_fail = None
    elif kind in FAIL_KINDS and not first_fail:
        first_fail = ts
    if kind == 'ok':
        for field, new in (('title', res['title']), ('content_hash', res['hash'])):
            old = site[field]
            if old is not None and old != new:
                db.execute("INSERT INTO changes(site_id,detected_at,field,old,new) VALUES(?,?,?,?,?)", (site['site_id'], ts, field, old, new))
    recovered = kind == 'ok' and site['state'] != 'listed' and not flag
    note = reason or ('復旧(自動で listed に戻った。人の確認推奨)' if recovered else (site['note'] if state != 'listed' else None))
    db.execute("INSERT INTO checks(site_id,checked_at,kind,http_status,final_url,error,state_after) VALUES(?,?,?,?,?,?,?)",
               (site['site_id'], ts, kind, res['http_status'], res['final_url'], res['error'], state))
    db.execute("""UPDATE sites SET flag=?, state=?, consecutive_failures=?, first_failure_at=?, last_checked_at=?,
                  last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END, last_kind=?, last_http_status=?, last_error=?, last_final_url=?,
                  title=CASE WHEN ? THEN ? ELSE title END, content_hash=CASE WHEN ? THEN ? ELSE content_hash END,
                  next_check_at=?, note=? WHERE site_id=?""",
               (flag, state, failures, first_fail, ts, kind == 'ok', ts, kind, res['http_status'], res['error'], res['final_url'],
                kind == 'ok', res['title'], kind == 'ok', res['hash'], next_check(kind, failures, now), note, site['site_id']))
    db.commit()


# ---- 実行 ----

class NetworkUnhealthy(Exception):
    """実行機のネットワーク障害の疑い。この回の結果は台帳に反映しない。"""


def network_healthy(probes, host_wait=HOST_WAIT):
    """既知の健全なホストのうち 1 つでも到達できれば健全。probes が空なら確認しない。"""
    if not probes:
        return True
    robots = Robots(lambda h: time.sleep(host_wait))
    for u in probes:
        if check_url(u, robots, host_wait=host_wait)['kind'] not in NETWORK_KINDS:
            return True
    return False


def run(db, limit=None, workers=3, host_wait=HOST_WAIT, now=None, log=print, probes=HEALTH_URLS):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sites = due_sites(db, now, limit)
    if not sites:
        return 0                      # 対象 0 件なら到達確認の外部アクセスもしない
    if not network_healthy(probes, host_wait):
        raise NetworkUnhealthy('到達確認先にも届かない。実行機のネットワーク障害とみなし、失敗に数えず中止')
    groups = defaultdict(list)       # ホスト単位で直列(同一ホストへの同時アクセスはしない)。順序=優先度
    for s in sites:
        groups[_strip(urllib.parse.urlsplit(s['url']).hostname or '')].append(s)

    def work(items):
        robots = Robots(lambda h: time.sleep(host_wait))
        out = []
        for s in items:
            try:
                res = check_url(s['url'], robots, host_wait=host_wait)
            except Exception as e:      # 1サイトの想定外エラーで全体を止めない
                res = {'kind': 'error', 'http_status': None, 'final_url': s['url'], 'error': repr(e)[:200], 'title': None, 'hash': None}
            out.append((s, res))
        return out

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(work, g) for g in groups.values()]):
            results += fut.result()
    # 実行中のネットワーク障害の検出: ネットワーク系失敗が多く、終了時の再プローブでも届かなければ記録しない
    net = sum(1 for _, r in results if r['kind'] in NETWORK_KINDS)
    if probes and net / len(results) >= NETWORK_FAIL_RATIO and not network_healthy(probes, host_wait):
        raise NetworkUnhealthy(f'実行中にネットワーク障害の疑い(ネットワーク系失敗 {net}/{len(results)})。この回は記録しない')
    for s, res in results:
        apply_result(db, s, res, now)
        log(f"{res['kind']:18} {res['http_status'] or '-'!s:4} {s['url']}")
    return len(results)


def confirm(db, key, reason, now=None):
    """人が確認して flag を解除する(site_id または url)。解除理由と日時を confirmations に残す。"""
    rows = db.execute("SELECT * FROM sites WHERE site_id=?", (key,)).fetchall() or db.execute("SELECT * FROM sites WHERE url=?", (key,)).fetchall()
    if not rows:
        raise KeyError(key)
    if len(rows) > 1:
        raise ValueError(f'同じ URL のサイトが {len(rows)} 件ある。site_id で指定する: ' + ', '.join(r['site_id'] for r in rows))
    r = rows[0]
    if not r['flag']:
        raise ValueError(f"{r['site_id']} に flag は付いていない(解除対象なし)")
    if r['state'] == 'hide_candidate':
        raise ValueError(f"{r['site_id']} は非表示候補(連続失敗 {r['consecutive_failures']} 回)。flag の解除ではなく、掲載の扱いを人が判断する")
    ts = now_iso(now or datetime.datetime.now(datetime.timezone.utc))
    db.execute("INSERT INTO confirmations(site_id,confirmed_at,flag,reason) VALUES(?,?,?,?)", (r['site_id'], ts, r['flag'], reason))
    state = 'listed' if r['consecutive_failures'] < REVIEW_AFTER else 'needs_review'
    db.execute("UPDATE sites SET flag=NULL, state=?, note=? WHERE site_id=?", (state, f'人が確認して解除({ts}): {reason}', r['site_id']))
    db.commit()
    return r['site_id']


def request_candidates(path):
    """取り下げ・訂正の申請(scripts/requests/import_requests.py が出す JSON Lines)。人が見る一覧に併記する。"""
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]


def candidates(db):
    return db.execute("SELECT site_id,url,name,state,flag,consecutive_failures,first_failure_at,last_checked_at,last_ok_at,last_kind,last_http_status,last_error,last_final_url,note "
                      "FROM sites WHERE state IN ('needs_review','hide_candidate') OR flag IS NOT NULL ORDER BY (state='hide_candidate') DESC, consecutive_failures DESC, site_id").fetchall()


def _q(v):
    if isinstance(v, int):
        return str(v)
    return 'NULL' if v is None else "'" + str(v).replace("'", "''") + "'"


def to_sql(db):
    """migrations/0017_recrawl.sql の site_checks 向け SQL。本番への適用は人が行う(自動では流さない)。"""
    cols = ['site_id', 'state', 'flag', 'consecutive_failures', 'last_checked_at', 'last_ok_at', 'last_kind', 'last_http_status', 'title', 'content_hash', 'next_check_at']
    lines = ['-- generated by scripts/recrawl/recrawl.py sql']
    for r in db.execute("SELECT * FROM sites WHERE last_checked_at IS NOT NULL ORDER BY site_id"):
        lines.append(f"INSERT OR REPLACE INTO site_checks({','.join(cols)}) VALUES({','.join(_q(r[c]) for c in cols)});")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('import', help='掲載済みサイトの JSON を取り込む'); p.add_argument('files', nargs='*')
    p = sub.add_parser('run', help='期限が来たサイトを優先度順に再巡回'); p.add_argument('--limit', type=int, default=50)
    p.add_argument('--workers', type=int, default=3); p.add_argument('--host-wait', type=float, default=HOST_WAIT)
    p = sub.add_parser('candidates', help='要確認・非表示候補の一覧(人が見る)。取り下げ・訂正の申請も併記'); p.add_argument('--json', action='store_true')
    p.add_argument('--requests-file', default=str(ROOT / 'var' / 'requests' / 'review.jsonl'))
    p = sub.add_parser('confirm', help='flag 付き(転送・内部アドレス)の要確認を人が確認して解除する'); p.add_argument('key', help='site_id または url'); p.add_argument('--reason', required=True)
    p = sub.add_parser('sql', help='site_checks 向けの SQL を出力'); p.add_argument('--out')
    p = sub.add_parser('changes', help='タイトル・本文ハッシュの変化の記録'); p.add_argument('--limit', type=int, default=50)
    a = ap.parse_args(argv)
    db = connect(a.db)
    try:
        if a.cmd == 'import':
            files = a.files or sorted(glob.glob(str(ROOT / 'data' / 'collection-*.json')))
            print(f'{import_sites(db, files)} 件を取り込み')
        elif a.cmd == 'run':
            try:
                print(f'{run(db, a.limit, a.workers, a.host_wait)} 件を確認')
            except NetworkUnhealthy as e:
                print(f'警告: {e}', file=sys.stderr); sys.exit(2)
        elif a.cmd == 'candidates':
            rows = candidates(db)
            reqs = request_candidates(a.requests_file)
            if a.json:
                print(json.dumps([dict(r) for r in rows] + reqs, ensure_ascii=False, indent=2))
            for r in ([] if a.json else rows):
                print(f"[{r['state']}{'/' + r['flag'] if r['flag'] else ''}] {r['site_id']} {r['url']} 失敗{r['consecutive_failures']}回 最終正常={r['last_ok_at']} 直近={r['last_kind']}/{r['last_http_status']} {r['note'] or ''}")
            for q in ([] if a.json else reqs):
                print(f"[{q['state']}] {q.get('site_id') or '掲載なし'} {q['url']} 申請#{q['request_id']} 理由: {q['reason']}")
            if not rows and not reqs and not a.json:
                print('候補なし')
        elif a.cmd == 'confirm':
            try:
                print(f'{confirm(db, a.key, a.reason)} の flag を解除')
            except KeyError:
                print('該当サイトなし', file=sys.stderr); sys.exit(1)
            except ValueError as e:
                print(f'拒否: {e}', file=sys.stderr); sys.exit(1)
        elif a.cmd == 'sql':
            s = to_sql(db)
            Path(a.out).write_text(s, encoding='utf-8') if a.out else print(s, end='')
        elif a.cmd == 'changes':
            for r in db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (a.limit,)):
                print(f"{r['detected_at']} {r['site_id']} {r['field']}: {r['old']!r} -> {r['new']!r}")

    finally:
        db.close()

if __name__ == '__main__':
    main()
