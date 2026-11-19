"""ファビコンの取得・縮小・保存(Issue #125)。手元/夜間ジョブで実行する。LLM なし、本番 D1・R2 には書かない。

台帳(SQLite, 既定 var/favicons.sqlite)に 1 サイト 1 行で状態を持ち、32px の WebP を内容ハッシュ名(<hash>.webp)で
IconStore(既定: ローカル var/favicons/)に置く。`sql` が sites.favicon_path 向けの UPDATE を出す(適用は人が行う)。
取得作法は scripts/pipeline/fetcher.py を再利用(UA=MiseResearchBot、robots 遵守、内部IP拒否、サイズ/時間上限)。
取れない/失敗時は何も出さない(代替アイコンは作らない)。SVG は不採用(無害化はしない)。
画像の検証と縮小に Pillow を使う: `uv run --with pillow python3 scripts/favicon/favicon.py ...`
"""
import argparse
import datetime
import hashlib
import io
import sqlite3
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import fetcher  # noqa: E402

DEFAULT_DB = ROOT / 'var' / 'favicons.sqlite'
DEFAULT_STORE = ROOT / 'var' / 'favicons'
ICON_SIZE = 32                 # 出力は 32x32
MAX_ICON_BYTES = 256 * 1024    # 取得する画像の上限
MAX_SIDE = 1024                # 元画像の一辺の上限(展開爆弾よけ)
MIN_SIDE = 16                  # これ未満は追跡ピクセル等とみなして不採用
MAX_CANDIDATES = 3             # 1 サイトで試す候補の上限(<link> 由来 + /favicon.ico)
HOST_WAIT = 2.0                # 同一ホストへのリクエスト間隔(秒)
MAX_REDIRECTS = 3
OK_INTERVAL_DAYS = 30          # 月次
RETRY_DAYS = 3                 # 一時的な失敗の再試行
ALLOWED_FORMATS = {'PNG', 'ICO', 'GIF', 'WEBP', 'JPEG', 'BMP'}   # SVG は含めない
# 再試行しても同じ結果になる失敗(月次に戻す)。これ以外の取得失敗は一時的として RETRY_DAYS 後に再試行
PERMANENT_ERRORS = {'ssrf_blocked', 'robots_disallowed', 'bad_url', 'bad_port', 'redirect_offsite', 'not_html'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS favicons (
  site_id TEXT PRIMARY KEY, url TEXT NOT NULL,
  icon_url TEXT, etag TEXT, last_modified TEXT, icon_hash TEXT,
  status TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT, next_check_at TEXT, changed_at TEXT);
"""


class IconError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}'); self.code = code; self.detail = detail


# ---- 保存先(差し替え可能) ----

class IconStore:
    """アイコンの置き場。実装は put/exists だけ。R2 実装は後続で同じ形で足す。"""
    def exists(self, name):
        raise NotImplementedError

    def put(self, name, data):
        """書いたら True。すでにあれば書かず False。"""
        raise NotImplementedError


class LocalDirStore(IconStore):
    def __init__(self, root):
        self.root = Path(root)

    def exists(self, name):
        return (self.root / name).is_file()

    def put(self, name, data):
        if self.exists(name):
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(self.root / name)
        return True


# ---- HTML から候補を選ぶ ----

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []; self.base = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or '') for k, v in attrs}
        if tag == 'base' and a.get('href') and self.base is None:
            self.base = a['href']
        elif tag == 'link' and a.get('href'):
            self.links.append(a)


def _max_declared(sizes):
    best = 0
    for tok in (sizes or '').lower().split():
        w, _, h = tok.partition('x')
        if w.isdigit() and h.isdigit():
            best = max(best, min(int(w), int(h)))
    return best


def icon_candidates(html, page_url):
    """HTML の <link> から候補 URL を優先順に返す(末尾に /favicon.ico)。
    優先順位: 0) rel=icon で宣言サイズが 32px 以上(小さい方から) / 1) apple-touch-icon / 2) rel=icon でサイズ宣言なし /
    3) rel=icon で 32px 未満(大きい方から) / 4) /favicon.ico。同順位は文書順。SVG(type・拡張子)と data: は除く。"""
    p = _LinkParser()
    try:
        p.feed(html[:200000]); p.close()
    except Exception:
        pass
    base = urllib.parse.urljoin(page_url, p.base) if p.base else page_url
    ranked = []
    for i, a in enumerate(p.links):
        rel = a.get('rel', '').lower().split()
        is_apple = any(t in ('apple-touch-icon', 'apple-touch-icon-precomposed') for t in rel)
        if not (is_apple or 'icon' in rel):
            continue
        href = a['href'].strip()
        if href.lower().startswith('data:') or 'svg' in a.get('type', '').lower():
            continue
        url = urllib.parse.urljoin(base, href)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path.lower().endswith('.svg'):
            continue
        size = _max_declared(a.get('sizes'))
        if is_apple:
            key = (1, 0, i)
        elif size >= ICON_SIZE:
            key = (0, size, i)
        elif size == 0:
            key = (2, 0, i)
        else:
            key = (3, -size, i)
        ranked.append((key, url))
    out = []
    for _, url in sorted(ranked):
        if url not in out:
            out.append(url)
    parts = urllib.parse.urlsplit(page_url)
    ico = f'{parts.scheme}://{parts.netloc}/favicon.ico'
    if ico not in out:
        out.append(ico)
    return out


# ---- 画像の検証と縮小 ----

def normalize_icon(data, content_type=''):
    """画像バイト列を検証し、32x32 の可逆 WebP にして返す。不採用なら IconError。"""
    from PIL import Image
    ct = (content_type or '').lower()
    head = data[:512].lstrip().lower()
    if 'svg' in ct or head.startswith((b'<svg', b'<?xml')):
        raise IconError('svg_rejected')
    if ct.startswith('text/') or 'html' in ct or head.startswith((b'<!doctype', b'<html', b'<')):
        raise IconError('not_image', ct or 'markup')
    if not data:
        raise IconError('not_image', 'empty')
    if len(data) > MAX_ICON_BYTES:
        raise IconError('too_large', f'{len(data)} bytes')
    Image.MAX_IMAGE_PIXELS = MAX_SIDE * MAX_SIDE * 4
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in ALLOWED_FORMATS:
            raise IconError('unsupported_format', str(im.format))
        if im.format == 'ICO' and im.info.get('sizes'):
            im.size = max(im.info['sizes'])   # 入っている中で最大の絵を使う
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            raise IconError('too_small', f'{w}x{h}')
        if max(w, h) > MAX_SIDE:
            raise IconError('too_large_dim', f'{w}x{h}')
        im.load()
        im = im.convert('RGBA')
    except IconError:
        raise
    except Exception as e:
        raise IconError('bad_image', f'{type(e).__name__}')
    if im.getchannel('A').getextrema()[1] == 0:
        raise IconError('blank')
    scale = ICON_SIZE / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new('RGBA', (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    canvas.paste(im, ((ICON_SIZE - nw) // 2, (ICON_SIZE - nh) // 2))
    buf = io.BytesIO()
    canvas.save(buf, 'WEBP', lossless=True, quality=100, method=6)
    return buf.getvalue()


def icon_name(webp):
    return hashlib.sha256(webp).hexdigest()[:32] + '.webp'


# ---- 取得 ----

class Session:
    """robots 遵守・ホスト別待ち・ホップごとの SSRF/robots 再判定つきの GET。"""
    def __init__(self, sleep=time.sleep, clock=time.monotonic, host_wait=HOST_WAIT):
        self.sleep = sleep; self.clock = clock; self.host_wait = host_wait
        self.last = {}
        self.robots = fetcher.RobotsCache(self.wait)

    def wait(self, host, extra=0):
        gap = max(self.host_wait, extra)
        now = self.clock()
        if host in self.last and now - self.last[host] < gap:
            self.sleep(gap - (now - self.last[host]))
        self.last[host] = self.clock()

    def get(self, url, max_body, headers=None, same_site_only=False):
        """戻り: (status, headers, body, final_url)。拒否・失敗は IconError。"""
        first = urllib.parse.urlsplit(url).hostname
        for _ in range(MAX_REDIRECTS + 1):
            parts = urllib.parse.urlsplit(url)
            origin = f'{parts.scheme}://{parts.netloc}'
            try:
                st, ok, delay = self.robots.check(origin, (parts.path or '/') + (f'?{parts.query}' if parts.query else ''))
                if st == 'unavailable':
                    raise IconError('robots_unavailable')
                if not ok:
                    raise IconError('robots_disallowed')
                self.wait(parts.hostname, delay)
                status, hdrs, body = fetcher.http_get(url, max_body, headers)
            except fetcher.FetchError as e:
                raise IconError(e.code, e.detail)
            if 300 <= status < 400 and hdrs.get('location'):
                nxt = urllib.parse.urljoin(url, hdrs['location'])
                np = urllib.parse.urlsplit(nxt)
                if np.scheme not in ('http', 'https'):
                    raise IconError('bad_url', nxt)
                if same_site_only and not fetcher.same_site(np.hostname or '', first or ''):
                    raise IconError('redirect_offsite', nxt)
                url = nxt; continue
            return status, hdrs, body, url
        raise IconError('too_many_redirects')


def fetch_favicon(session, site_url, prev=None):
    """1 サイト分。戻り dict: status(ok|unchanged|none|error), icon_url, etag, last_modified, webp, error。"""
    prev = prev or {}
    # 1) 前回の画像 URL に条件付き GET(ページを取らずに済む)
    if prev.get('icon_url'):
        cond = {}
        if prev.get('etag'):
            cond['If-None-Match'] = prev['etag']
        if prev.get('last_modified'):
            cond['If-Modified-Since'] = prev['last_modified']
        try:
            status, hdrs, body, final = session.get(prev['icon_url'], MAX_ICON_BYTES + 1, cond)
            if status == 304:
                return {'status': 'unchanged', 'icon_url': prev['icon_url']}
            if status == 200:
                webp = normalize_icon(body, hdrs.get('content-type', ''))
                return {'status': 'ok', 'icon_url': final, 'etag': hdrs.get('etag'), 'last_modified': hdrs.get('last-modified'), 'webp': webp}
        except IconError:
            pass   # 取り直し(下の発見)に進む
    # 2) ページから候補を探す
    try:
        status, hdrs, body, final = session.get(site_url, fetcher.MAX_BODY, same_site_only=True)
    except IconError as e:
        return {'status': 'error', 'error': e.code}
    if status == 200 and 'html' in hdrs.get('content-type', 'html').lower():
        urls = icon_candidates(fetcher.decode_body(body, hdrs), final)
    else:
        parts = urllib.parse.urlsplit(final)
        urls = [f'{parts.scheme}://{parts.netloc}/favicon.ico']
    last_err = 'no_icon'
    for u in urls[:MAX_CANDIDATES]:
        try:
            st, h, b, fin = session.get(u, MAX_ICON_BYTES + 1)
            if st != 200:
                last_err = last_err if last_err != 'no_icon' else f'http_{st}'; continue
            webp = normalize_icon(b, h.get('content-type', ''))
            return {'status': 'ok', 'icon_url': fin, 'etag': h.get('etag'), 'last_modified': h.get('last-modified'), 'webp': webp}
        except IconError as e:
            last_err = e.code if last_err == 'no_icon' else last_err
            if e.code in ('ssrf_blocked', 'robots_unavailable'):
                return {'status': 'error', 'error': e.code}
    return {'status': 'none', 'error': last_err}


# ---- 台帳 ----

def now_iso(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path):
    path = Path(path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path)); db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def import_sites(db, rows):
    """rows: [(site_id, url)]。URL だけ更新し、状態は保つ。"""
    for sid, url in rows:
        db.execute('INSERT INTO favicons(site_id,url) VALUES(?,?) ON CONFLICT(site_id) DO UPDATE SET url=excluded.url', (sid, url))
    db.commit()
    return len(rows)


def import_from_recrawl(db, recrawl_path):
    """再巡回台帳(scripts/recrawl)で最後に取得に成功しており、いま listed のサイトだけを対象にする(死んだ・要確認のサイトは取りに行かない)。"""
    src = sqlite3.connect(f'file:{recrawl_path}?mode=ro', uri=True)
    try:
        rows = src.execute("SELECT site_id, url FROM sites WHERE state='listed' AND last_ok_at IS NOT NULL").fetchall()
    finally:
        src.close()
    return import_sites(db, rows)


def due_sites(db, now, limit):
    return db.execute('SELECT * FROM favicons WHERE next_check_at IS NULL OR next_check_at <= ? ORDER BY next_check_at IS NOT NULL, next_check_at, site_id LIMIT ?',
                      (now_iso(now), limit)).fetchall()


def _plus(now, days):
    return now_iso(now + datetime.timedelta(days=days))


def record(db, store, row, res, now):
    """結果を台帳と保存先に反映する。内容ハッシュが同じなら保存先に書かない。戻り: 'changed'|'same'|'unchanged'|'none'|'error'"""
    sid = row['site_id']; t = now_iso(now)
    if res['status'] == 'unchanged':
        db.execute("UPDATE favicons SET status='ok', consecutive_failures=0, last_checked_at=?, next_check_at=? WHERE site_id=?", (t, _plus(now, OK_INTERVAL_DAYS), sid))
        out = 'unchanged'
    elif res['status'] == 'ok':
        name = icon_name(res['webp'])
        out = 'same' if row['icon_hash'] == name else 'changed'
        if out == 'changed':
            store.put(name, res['webp'])   # 既に同じ画像(他サイトの同一アイコン)があれば書かれない
        db.execute("UPDATE favicons SET icon_url=?, etag=?, last_modified=?, icon_hash=?, status='ok', consecutive_failures=0, last_checked_at=?, next_check_at=?, changed_at=COALESCE(?, changed_at) WHERE site_id=?",
                   (res['icon_url'], res.get('etag'), res.get('last_modified'), name, t, _plus(now, OK_INTERVAL_DAYS), t if out == 'changed' else None, sid))
    elif res['status'] == 'none':
        db.execute("UPDATE favicons SET status='none', last_checked_at=?, next_check_at=? WHERE site_id=?", (t, _plus(now, OK_INTERVAL_DAYS), sid))
        out = 'none'
    else:
        days = OK_INTERVAL_DAYS if res.get('error') in PERMANENT_ERRORS else RETRY_DAYS
        db.execute('UPDATE favicons SET status=?, consecutive_failures=consecutive_failures+1, last_checked_at=?, next_check_at=? WHERE site_id=?',
                   (res.get('error') or 'error', t, _plus(now, days), sid))
        out = 'error'
    db.commit()
    return out


def run(db, store, now=None, limit=50, session=None, log=print):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    session = session or Session()
    counts = {}
    for row in due_sites(db, now, limit):
        try:
            res = fetch_favicon(session, row['url'], dict(row))
        except Exception as e:   # 1 サイトの想定外の例外で全体を止めない
            res = {'status': 'error', 'error': type(e).__name__}
        out = record(db, store, row, res, now)
        counts[out] = counts.get(out, 0) + 1
        log(f"{row['site_id']}\t{out}\t{res.get('error') or ''}")
    return counts


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def sql_updates(db, since=None):
    """sites.favicon_path へ入れる UPDATE。値が同じなら書かない(D1 の書き込み行数を使わない)。取れていないサイトは触れない。"""
    q = 'SELECT site_id, icon_hash FROM favicons WHERE icon_hash IS NOT NULL'
    args = ()
    if since:
        q += ' AND changed_at >= ?'; args = (since,)
    lines = [f"UPDATE sites SET favicon_path={_q(r['icon_hash'])} WHERE id={_q(r['site_id'])} AND favicon_path IS NOT {_q(r['icon_hash'])};"
             for r in db.execute(q + ' ORDER BY site_id', args)]
    return '\n'.join(lines) + ('\n' if lines else '')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('import', help='対象サイトを台帳へ'); p.add_argument('--recrawl', help='再巡回台帳(var/recrawl.sqlite)')
    p.add_argument('--sites', nargs='*', default=[], help='[{id,url}] の JSON')
    p = sub.add_parser('run', help='期限が来たサイトを取得(--apply が無ければ計画だけ。ネットワーク・書き込みなし)')
    p.add_argument('--limit', type=int, default=50); p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p = sub.add_parser('sql', help='sites.favicon_path 向けの UPDATE を出す'); p.add_argument('--out'); p.add_argument('--since', help='changed_at がこれ以降のものだけ(ISO)')
    sub.add_parser('status', help='台帳の集計')
    a = ap.parse_args(argv)
    db = connect(a.db)
    if a.cmd == 'import':
        import json
        n = 0
        for f in a.sites:
            n += import_sites(db, [(r['id'], r['url']) for r in json.loads(Path(f).read_text(encoding='utf-8')) if r.get('id') and r.get('url')])
        if a.recrawl:
            n += import_from_recrawl(db, a.recrawl)
        print(f'imported={n}')
    elif a.cmd == 'run':
        now = datetime.datetime.now(datetime.timezone.utc)
        if not a.apply:
            rows = due_sites(db, now, a.limit)
            print(f'[dry-run] 取得対象 {len(rows)} 件(--apply で実行。実サイトにアクセスする)')
            for r in rows[:20]:
                print(f"  {r['site_id']}\t{r['url']}")
            return 0
        print('done ' + str(run(db, LocalDirStore(a.store_dir), now, a.limit)))
    elif a.cmd == 'sql':
        text = sql_updates(db, a.since)
        if a.out:
            Path(a.out).write_text(text, encoding='utf-8')
            print(f'wrote {a.out} ({text.count(chr(10))} 行)')
        else:
            sys.stdout.write(text)
    else:
        for r in db.execute('SELECT status, COUNT(*) n FROM favicons GROUP BY status ORDER BY n DESC'):
            print(f"{r['status'] or 'pending'}\t{r['n']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
