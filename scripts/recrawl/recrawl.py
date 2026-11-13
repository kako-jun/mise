"""登録済みサイトの再巡回(手元実行)。決定論のみ、LLM なし、本番 D1 には書かない。

台帳(SQLite, 既定 var/recrawl.sqlite)に結果と状態を持ち、`candidates` で人が見る一覧、`sql` で
site_checks(migrations/0017_recrawl.sql)向けの SQL を出す。自動削除・紹介文の書き換えはしない。
取得作法は scripts/pipeline/fetcher.py を再利用(UA=MiseResearchBot、robots 遵守、内部IP拒否)。
"""
import argparse
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
NEUTRAL_KINDS = {'robots_disallowed', 'robots_unavailable', 'http_denied', 'ssrf_blocked', 'error'}
# ok / redirect_offsite はそれぞれ成功 / 要確認(転送先が別ドメイン=乗っ取り・移転の疑い)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  site_id TEXT PRIMARY KEY, url TEXT NOT NULL, name TEXT,
  state TEXT NOT NULL DEFAULT 'listed',            -- listed | needs_review | hide_candidate
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  first_failure_at TEXT, last_checked_at TEXT, last_ok_at TEXT,
  last_kind TEXT, last_http_status INTEGER, last_error TEXT, last_final_url TEXT,
  title TEXT, content_hash TEXT, next_check_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, checked_at TEXT NOT NULL, kind TEXT NOT NULL,
  http_status INTEGER, final_url TEXT, error TEXT, state_after TEXT);
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


def _same_domain(a, b):
    return hostnames.registrable(_strip(a)) == hostnames.registrable(_strip(b))


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


def check_url(url, robots, sleep=time.sleep, host_wait=HOST_WAIT):
    """1サイトのトップを robots 確認の上で取得し分類する。戻り dict: kind, http_status, final_url, error, title, hash"""
    out = {'kind': 'error', 'http_status': None, 'final_url': url, 'error': None, 'title': None, 'hash': None}
    first_host = urllib.parse.urlsplit(url).hostname or ''
    cur = url
    for _ in range(fetcher.MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(cur)
        origin = f'{parts.scheme}://{parts.netloc}'
        try:
            st, ok, delay = robots.check(origin, parts.path or '/')
        except fetcher.FetchError as e:
            out.update(kind=classify_error(e), error=e.detail[:200]); return out
        if st == 'unavailable':
            out['kind'] = 'robots_unavailable'; return out
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
            cur = nxt; continue
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


def next_state(kind, failures, first_failure_at, now):
    """(新しい連続失敗回数, 状態, 理由) を返す。自動削除はない。"""
    if kind == 'ok':
        return 0, 'listed', None
    if kind == 'redirect_offsite':
        return failures, 'needs_review', '別ドメインへ転送(移転・乗っ取り・売却の疑い)'
    if kind in NEUTRAL_KINDS:
        return failures, None, None            # 状態は据え置き
    failures += 1
    if failures >= HIDE_AFTER and first_failure_at and (now - parse_iso(first_failure_at)).days >= HIDE_MIN_DAYS:
        return failures, 'hide_candidate', f'連続失敗{failures}回・{HIDE_MIN_DAYS}日以上'
    if failures >= REVIEW_AFTER:
        return failures, 'needs_review', f'連続失敗{failures}回'
    return failures, 'listed', None


def next_check(kind, failures, now):
    if kind == 'ok':
        days = OK_INTERVAL_DAYS
    elif kind in NEUTRAL_KINDS:
        days = 7
    elif kind == 'redirect_offsite':
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
    failures, state, reason = next_state(kind, site['consecutive_failures'], site['first_failure_at'], now)
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
    recovered = kind == 'ok' and site['state'] != 'listed'
    note = reason or ('復旧(自動で listed に戻った。人の確認推奨)' if recovered else (site['note'] if state != 'listed' else None))
    db.execute("INSERT INTO checks(site_id,checked_at,kind,http_status,final_url,error,state_after) VALUES(?,?,?,?,?,?,?)",
               (site['site_id'], ts, kind, res['http_status'], res['final_url'], res['error'], state))
    db.execute("""UPDATE sites SET state=?, consecutive_failures=?, first_failure_at=?, last_checked_at=?,
                  last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END, last_kind=?, last_http_status=?, last_error=?, last_final_url=?,
                  title=CASE WHEN ? THEN ? ELSE title END, content_hash=CASE WHEN ? THEN ? ELSE content_hash END,
                  next_check_at=?, note=? WHERE site_id=?""",
               (state, failures, first_fail, ts, kind == 'ok', ts, kind, res['http_status'], res['error'], res['final_url'],
                kind == 'ok', res['title'], kind == 'ok', res['hash'], next_check(kind, failures, now), note, site['site_id']))
    db.commit()


# ---- 実行 ----

def run(db, limit=None, workers=3, host_wait=HOST_WAIT, now=None, log=print):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sites = due_sites(db, now, limit)
    groups = defaultdict(list)       # ホスト単位で直列(同一ホストへの同時アクセスはしない)。順序=優先度
    for s in sites:
        groups[_strip(urllib.parse.urlsplit(s['url']).hostname or '')].append(s)

    def work(items):
        robots = fetcher.RobotsCache(lambda h: time.sleep(host_wait))
        out = []
        for s in items:
            try:
                res = check_url(s['url'], robots, host_wait=host_wait)
            except Exception as e:      # 1サイトの想定外エラーで全体を止めない
                res = {'kind': 'error', 'http_status': None, 'final_url': s['url'], 'error': repr(e)[:200], 'title': None, 'hash': None}
            out.append((s, res))
        return out

    n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(work, g) for g in groups.values()]):
            for s, res in fut.result():
                apply_result(db, s, res, now)
                n += 1
                log(f"{res['kind']:18} {res['http_status'] or '-'!s:4} {s['url']}")
    return n


def candidates(db):
    return db.execute("SELECT site_id,url,name,state,consecutive_failures,first_failure_at,last_checked_at,last_ok_at,last_kind,last_http_status,last_error,last_final_url,note "
                      "FROM sites WHERE state IN ('needs_review','hide_candidate') ORDER BY (state='hide_candidate') DESC, consecutive_failures DESC, site_id").fetchall()


def _q(v):
    if isinstance(v, int):
        return str(v)
    return 'NULL' if v is None else "'" + str(v).replace("'", "''") + "'"


def to_sql(db):
    """migrations/0017_recrawl.sql の site_checks 向け SQL。本番への適用は人が行う(自動では流さない)。"""
    cols = ['site_id', 'state', 'consecutive_failures', 'last_checked_at', 'last_ok_at', 'last_kind', 'last_http_status', 'title', 'content_hash', 'next_check_at']
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
    sub.add_parser('candidates', help='要確認・非表示候補の一覧(人が見る)').add_argument('--json', action='store_true')
    p = sub.add_parser('sql', help='site_checks 向けの SQL を出力'); p.add_argument('--out')
    p = sub.add_parser('changes', help='タイトル・本文ハッシュの変化の記録'); p.add_argument('--limit', type=int, default=50)
    a = ap.parse_args(argv)
    db = connect(a.db)
    if a.cmd == 'import':
        files = a.files or sorted(glob.glob(str(ROOT / 'data' / 'collection-*.json')))
        print(f'{import_sites(db, files)} 件を取り込み')
    elif a.cmd == 'run':
        print(f'{run(db, a.limit, a.workers, a.host_wait)} 件を確認')
    elif a.cmd == 'candidates':
        rows = candidates(db)
        if a.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        for r in ([] if a.json else rows):
            print(f"[{r['state']}] {r['site_id']} {r['url']} 失敗{r['consecutive_failures']}回 最終正常={r['last_ok_at']} 直近={r['last_kind']}/{r['last_http_status']} {r['note'] or ''}")
        if not rows and not a.json:
            print('候補なし')
    elif a.cmd == 'sql':
        s = to_sql(db)
        Path(a.out).write_text(s, encoding='utf-8') if a.out else print(s, end='')
    elif a.cmd == 'changes':
        for r in db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (a.limit,)):
            print(f"{r['detected_at']} {r['site_id']} {r['field']}: {r['old']!r} -> {r['new']!r}")


if __name__ == '__main__':
    main()
