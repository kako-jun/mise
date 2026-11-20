"""ファビコンの取得・縮小・保存(Issue #125)。手元/夜間ジョブで実行する。LLM なし、本番 D1 には書かない。R2 へは --store r2 を明示したときだけ(既定は --local の模擬)。

台帳(SQLite, 既定 var/favicons.sqlite)に 1 サイト 1 行で状態を持ち、32px の WebP をサイト固定のキー
(<sha256(site_id)先頭32桁>.webp。更新は上書き・1 サイト 1 枚・世代管理なし。サムネイルと同じ規則。#163)で
IconStore(既定: ローカル var/favicons/)に置く。`sql` が sites.favicon_path / favicon_v 向けの UPDATE を出す(適用は人が行う)。
`delete --site <id>` で 1 サイト分を保存先から消す(取り下げ・除外)。内容ハッシュ名(旧)からの移行は scripts/favicon/rekey.py。
取得作法は scripts/pipeline/fetcher.py を再利用(UA=MiseResearchBot、robots 遵守、内部IP拒否、サイズ/時間上限)。
取れない/失敗時は何も出さない(代替アイコンは作らない)。SVG は不採用(無害化はしない)。
画像の検証と縮小に Pillow を使う: `uv run --with pillow python3 scripts/favicon/favicon.py ...`
"""
import argparse
import datetime
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import fetcher  # noqa: E402

DEFAULT_DB = ROOT / 'var' / 'favicons.sqlite'
DEFAULT_STORE = ROOT / 'var' / 'favicons'
R2_BUCKET = 'mise-media'      # wrangler.jsonc の r2_buckets と同じ名前
R2_PREFIX = 'icon/'           # 配信ルート /icon/<k>.webp のキー(<k> = sha256(site_id) 先頭 32 桁。サイト固定)
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
  last_checked_at TEXT, next_check_at TEXT, changed_at TEXT,
  exclude INTEGER NOT NULL DEFAULT 0, exclude_reason TEXT);   -- 手動の除外・取り下げ(delete)。取らず、sql には favicon 列を NULL にする UPDATE を出す
-- どの保存先の、どのサイトのキー(<sha256(site_id)先頭32桁>.webp)に、どの内容(favicons.icon_hash)を最後に置いたかの印。保存先ごとに別(local / r2-local / r2-remote)。
-- キーはサイト固定で更新は上書きなので、印は「そのキーに置いた最後の内容」を 1 つだけ持つ。旧い台帳には connect() が自動で足す(旧 icon_stored は内容ハッシュ名の印で
-- 新キーを置いた証拠にならないので引き継がない。置いた印は rekey.py か run が付ける)。
CREATE TABLE IF NOT EXISTS icon_put (site_id TEXT NOT NULL, store TEXT NOT NULL, content TEXT, PRIMARY KEY (site_id, store));
"""
STORE_LABELS = ('local', 'r2-local', 'r2-remote')


class IconError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}'); self.code = code; self.detail = detail


# ---- 保存先(差し替え可能) ----

class IconStore:
    """アイコンの置き場。実装は put/exists/delete。R2 は R2Store。名前はサイト固定のキー(<k>.webp)で、put は同じ名前を上書きする。
    label は台帳の印(icon_put.store)の名前。trust_ledger が真なら、保存先に問い合わせず台帳の印だけで「置いてある」と判断する。"""
    label = 'local'
    trust_ledger = False

    def exists(self, name):
        raise NotImplementedError

    def put(self, name, data):
        """書いたら True(同じ名前は上書き)。この実行中に同じ名前を置いていれば書かず False。"""
        raise NotImplementedError

    def delete(self, name):
        raise NotImplementedError


class LocalDirStore(IconStore):
    def __init__(self, root):
        self.root = Path(root)

    def exists(self, name):
        return (self.root / name).is_file()

    def put(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(self.root / name)   # 同じ名前は上書き(更新で古い画像は残らない)
        return True

    def delete(self, name):
        f = self.root / name
        if f.is_file():
            f.unlink(); return True
        return False


class R2Store(IconStore):
    """R2 バケットへ wrangler CLI で置く。`run --apply --store r2` を明示したときだけ使う(既定は LocalDirStore)。

    - 既定は本番ではなく --local(wrangler dev --local と同じローカル模擬の R2)。本番(--remote)へは `--remote` を明示したときだけ。
    - exists は既定で R2 に問い合わせず、台帳(icon_hash)を信じて True を返す(wrangler の起動は 1 回数秒で、月次の 304 を全件確認すると遅い)。
      R2 から画像が消えた/別バケットへ移した疑いのときだけ verify=True(--r2-verify)で `wrangler r2 object get` により 1 件ずつ確かめる。
    - put は同じ名前(サイト固定のキー)への上書きなので何度やっても同じ結果。この実行中に置いた名前は記憶して 2 度呼ばない。
    wrangler は `runner(args)`(戻り値は終了コード)で呼ぶ。テストでは runner を差し替え、実際には呼ばない。"""
    def __init__(self, bucket=R2_BUCKET, remote=False, runner=None, cwd=None, verify=False):
        self.verify = verify; self.trust_ledger = not verify; self.label = 'r2-remote' if remote else 'r2-local'; self.bucket = bucket; self.remote = remote; self.cwd = cwd or ROOT
        self.runner = runner or self._run
        self.known = set()

    def _run(self, args):
        return subprocess.run(['npx', 'wrangler'] + args, cwd=self.cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120).returncode

    def _flag(self):
        return ['--remote'] if self.remote else ['--local']

    def _key(self, name):
        return f'{self.bucket}/{R2_PREFIX}{name}'

    def exists(self, name):
        if name in self.known or not self.verify:
            return True
        ok = self.runner(['r2', 'object', 'get', self._key(name), '--pipe'] + self._flag()) == 0
        if ok:
            self.known.add(name)
        return ok

    def put(self, name, data):
        if name in self.known:
            return False
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / name
            f.write_bytes(data)
            rc = self.runner(['r2', 'object', 'put', self._key(name), '--file', str(f), '--content-type', 'image/webp'] + self._flag())
        if rc != 0:
            raise OSError(f'wrangler r2 object put が失敗(終了コード {rc})')   # run() が 1 サイト分の store_failed として続行する
        self.known.add(name)
        return True

    def delete(self, name):
        """R2 から 1 本消す(サイト ID からキーが決まるので、他サイトの参照確認は要らない)。"""
        rc = self.runner(['r2', 'object', 'delete', self._key(name)] + self._flag())
        if rc != 0:
            raise OSError(f'wrangler r2 object delete が失敗(終了コード {rc})')
        self.known.discard(name)
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

def _check_dims(size):
    w, h = size
    if min(w, h) < MIN_SIDE:
        raise IconError('too_small', f'{w}x{h}')
    if max(w, h) > MAX_SIDE:
        raise IconError('too_large_dim', f'{w}x{h}')


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
    saved_max = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_SIDE * MAX_SIDE * 4   # この関数の中だけ(終わったら戻す)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)   # 警告も拒否扱い
            im = Image.open(io.BytesIO(data), formats=sorted(ALLOWED_FORMATS))
            if im.format == 'ICO' and im.info.get('sizes'):
                im.size = max(im.info['sizes'])   # 入っている中で最大の絵を使う
            _check_dims(im.size)
            im.load()
            _check_dims(im.size)   # 埋め込み画像の実寸で取り直す(宣言サイトと違うことがある)
            im = im.convert('RGBA')
    except IconError:
        raise
    except Exception as e:
        raise IconError('bad_image', f'{type(e).__name__}')
    finally:
        Image.MAX_IMAGE_PIXELS = saved_max
    w, h = im.size
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
    """画像の内容ハッシュ名(<hash>.webp)。変更検知用(favicons.icon_hash)。旧データの保存先のキーでもあったが、いまの保存先のキーはサイト固定で別(site_name)。"""
    return hashlib.sha256(webp).hexdigest()[:32] + '.webp'


def site_key(site_id):
    """保存先・配信のキー(拡張子なし 32 桁 16 進)。サイト ID から決まる(sha256 の先頭 32 桁。サムネイルと同じ)。"""
    return hashlib.sha256(str(site_id).encode('utf-8')).hexdigest()[:32]


def site_name(site_id):
    """sites.favicon_path の値・R2 の icon/ 以下の名前。<キー>.webp"""
    return site_key(site_id) + '.webp'


def icon_v(content_name):
    """sites.favicon_v の値(表示のバージョン。URL の ?v=)。内容ハッシュ名 <hash>.webp の hash の先頭 8 桁。"""
    return str(content_name).split('.')[0][:8]


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
        for hop in range(MAX_REDIRECTS + 1):
            parts = urllib.parse.urlsplit(url)
            origin = f'{parts.scheme}://{parts.netloc}'
            try:
                st, ok, delay = self.robots.check(origin, (parts.path or '/') + (f'?{parts.query}' if parts.query else ''))
                if st == 'unavailable':
                    raise IconError('robots_unavailable')
                if not ok:
                    raise IconError('robots_disallowed')
                self.wait(parts.hostname, delay)
                status, hdrs, body = fetcher.http_get(url, max_body, headers if hop == 0 else None)   # 条件付きヘッダは元 URL だけ
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


def _clean(v):
    return str(v).replace('\r', '').replace('\n', '')   # ヘッダ注入よけ(サーバーが返した値を curl へ戻すため)


def fetch_favicon(session, site_url, prev=None):
    """1 サイト分。戻り dict: status(ok|unchanged|none|error), icon_url, etag, last_modified, webp, error。"""
    prev = prev or {}
    # 1) 前回の画像 URL に条件付き GET(ページを取らずに済む)
    if prev.get('icon_url'):
        cond = {}
        if prev.get('etag'):
            cond['If-None-Match'] = _clean(prev['etag'])
        if prev.get('last_modified'):
            cond['If-Modified-Since'] = _clean(prev['last_modified'])
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
    if status >= 500 or status == 429:
        return {'status': 'error', 'error': f'http_{status}'}   # 一時的。/favicon.ico へは進まない
    if status == 200 and 'html' in hdrs.get('content-type', 'html').lower():
        urls = icon_candidates(fetcher.decode_body(body, hdrs), final)
    else:
        parts = urllib.parse.urlsplit(final)
        urls = [f'{parts.scheme}://{parts.netloc}/favicon.ico']
    last_err = 'no_icon'; blocked = []
    for u in urls[:MAX_CANDIDATES]:
        try:
            st, h, b, fin = session.get(u, MAX_ICON_BYTES + 1)
            if st != 200:
                last_err = last_err if last_err != 'no_icon' else f'http_{st}'
                blocked.append(False); continue
            webp = normalize_icon(b, h.get('content-type', ''))
            return {'status': 'ok', 'icon_url': fin, 'etag': h.get('etag'), 'last_modified': h.get('last-modified'), 'webp': webp}
        except IconError as e:
            last_err = e.code if last_err == 'no_icon' else last_err
            blocked.append(e.code in ('ssrf_blocked', 'robots_unavailable'))
            continue
    if blocked and all(blocked):   # 全候補が内部アドレス・robots 取得不能で試せなかった: 取れなかったのでなく保留
        return {'status': 'error', 'error': last_err}
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
    for col in ('exclude INTEGER NOT NULL DEFAULT 0', 'exclude_reason TEXT'):
        try:
            db.execute('ALTER TABLE favicons ADD COLUMN ' + col)   # 旧い台帳に足す
        except sqlite3.OperationalError:
            pass
    return db


def open_readonly(path):
    """読み取り専用で開く(台帳もディレクトリも作らない)。無ければ None。"""
    if not Path(path).is_file():
        return None
    db = sqlite3.connect('file:' + urllib.parse.quote(os.path.abspath(path)) + '?mode=ro', uri=True); db.row_factory = sqlite3.Row
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


def _has_marks_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='icon_put'").fetchone() is not None


def _has_exclude(db):
    return any(r[1] == 'exclude' for r in db.execute('PRAGMA table_info(favicons)'))


def _marked(db, sid, label, content=None):
    """この保存先のこのサイトのキーに、(content が与えられればその内容を)置いた印があるか。"""
    try:
        r = db.execute('SELECT content FROM icon_put WHERE site_id=? AND store=?', (sid, label)).fetchone()
    except sqlite3.OperationalError:   # 旧い台帳を読み取り専用で開いたとき(表が無い)
        return False
    return r is not None and (content is None or r['content'] == content)


def _mark(db, sid, label, content):
    db.execute('INSERT INTO icon_put(site_id, store, content) VALUES(?,?,?) ON CONFLICT(site_id, store) DO UPDATE SET content=excluded.content', (sid, label, content))


def _unmark(db, sid, label):
    db.execute('DELETE FROM icon_put WHERE site_id=? AND store=?', (sid, label))


def due_sites(db, now, limit, store=None):
    """期限が来たサイト(除外は含めない)。store(IconStore)を渡すと、期限前でも「画像は取れているのにその保存先のキーに現在の内容を置いた印が無い」サイトも対象にする(ローカルで置いた後の本番投入など)。"""
    ex = ' AND exclude=0' if _has_exclude(db) else ''
    args = [now_iso(now)]
    cond = 'next_check_at IS NULL OR next_check_at <= ?'
    if store is not None and _has_marks_table(db):
        cond += (" OR (status='ok' AND icon_hash IS NOT NULL AND NOT EXISTS (SELECT 1 FROM icon_put s WHERE s.site_id=favicons.site_id AND s.store=? AND s.content=favicons.icon_hash))")
        args.append(store.label)   # 印の表が無い旧い台帳(読み取り専用)は、印なしの期限判定だけ
    return db.execute(f'SELECT * FROM favicons WHERE ({cond}){ex} ORDER BY next_check_at IS NOT NULL, next_check_at, site_id LIMIT ?', (*args, limit)).fetchall()


def _plus(now, days):
    return now_iso(now + datetime.timedelta(days=days))


def record(db, store, row, res, now):
    """結果を台帳と保存先に反映する。サイト固定のキーへ上書きする(内容が同じ印が R2 にあれば書かない)。戻り: 'changed'|'same'|'unchanged'|'none'|'error'"""
    sid = row['site_id']; t = now_iso(now)
    if res['status'] == 'unchanged':
        db.execute("UPDATE favicons SET status='ok', consecutive_failures=0, last_checked_at=?, next_check_at=? WHERE site_id=?", (t, _plus(now, OK_INTERVAL_DAYS), sid))
        out = 'unchanged'
    elif res['status'] == 'ok':
        name = icon_name(res['webp'])   # 内容ハッシュ名(変更検知。保存先のキーはサイト固定)
        out = 'same' if row['icon_hash'] == name else 'changed'
        if not (store.trust_ledger and _marked(db, sid, store.label, name)):   # R2 は同じ内容の印があれば put しない(wrangler 起動を省く)。--r2-verify・ローカルは常に put
            store.put(site_name(sid), res['webp'])   # 同じキーへの上書き(古い画像は残らない)。同じ画像でも別サイトは別キー(重複排除しない)
        _mark(db, sid, store.label, name)   # この保存先のこのサイトのキーに置いた内容の印(put が例外なら印は付かない)
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


def _present(db, store, row):
    """この保存先のサイト固定のキーに、現在の内容があるか。印が無ければ無い。trust_ledger でない保存先(ローカル・R2 の確認つき)は実在も確かめる。"""
    if not _marked(db, row['site_id'], store.label, row['icon_hash']):
        return False
    return store.trust_ledger or store.exists(site_name(row['site_id']))


def run(db, store, now=None, limit=50, session=None, log=print):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    session = session or Session()
    counts = {}
    for row in due_sites(db, now, limit, store):
        try:
            res = fetch_favicon(session, row['url'], dict(row))
            if res['status'] == 'unchanged' and row['icon_hash'] and not _present(db, store, row):
                # 304 だが、この保存先のキーに現在の内容の印が無い(消えた・別の保存先へ移した・ローカルでしか置いていない): 条件なしで取り直して置く
                res = fetch_favicon(session, row['url'], dict(row, etag=None, last_modified=None))
        except ImportError:
            raise   # Pillow が無い等は 1 サイトの失敗ではない。握りつぶさず全体を止める
        except Exception as e:   # 1 サイトの想定外の例外で全体を止めない
            res = {'status': 'error', 'error': type(e).__name__}
        try:
            out = record(db, store, row, res, now)
        except OSError as e:   # 保存先に書けない(ディスク満杯等): このサイトだけ失敗として続行
            out = record(db, store, row, {'status': 'error', 'error': 'store_failed'}, now)
            res = {'error': f'store_failed: {type(e).__name__}'}
        counts[out] = counts.get(out, 0) + 1
        log(f"{row['site_id']}\t{out}\t{res.get('error') or ''}")
    return counts


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def sql_updates(db, since=None, store=None):
    """sites.favicon_path / favicon_v へ入れる UPDATE。favicon_path はサイト固定(<sha256(site_id) 先頭32桁>.webp)、favicon_v は内容ハッシュの先頭 8 桁。
    **冪等判定は favicon_path・favicon_v のどちらかが違うとき**(内容が変わったときと、v は同じで path だけ旧名の行を直すとき。合っている行は書かない。D1 の書き込み行数を使わない)。取れていないサイトは触れない。
    store(ラベル名)を渡すと、その保存先のそのキーに現在の内容を置いた印のある画像だけを出す(D1 が指す画像が配信元に無い状態を作らない)。None は全件。
    除外・取り下げ(delete)のサイトは、favicon 列を NULL にする UPDATE を出す(何度流しても安全)。"""
    ex = ' AND exclude=0' if _has_exclude(db) else ''
    q = f'SELECT site_id, icon_hash FROM favicons WHERE icon_hash IS NOT NULL{ex}'
    args = ()
    if since:
        q += ' AND changed_at >= ?'; args += (since,)
    rows = db.execute(q + ' ORDER BY site_id', args).fetchall()
    if store:
        rows = [r for r in rows if _marked(db, r['site_id'], store, r['icon_hash'])]   # 印の表が無い旧い台帳(読み取り専用)は印が 1 つも無い
    lines = [f"UPDATE sites SET favicon_path={_q(site_name(r['site_id']))}, favicon_v={_q(icon_v(r['icon_hash']))} "
             f"WHERE id={_q(r['site_id'])} AND (favicon_path IS NOT {_q(site_name(r['site_id']))} OR favicon_v IS NOT {_q(icon_v(r['icon_hash']))});" for r in rows]
    if ex:
        for r in db.execute('SELECT site_id FROM favicons WHERE exclude=1 ORDER BY site_id'):
            lines.append(f"UPDATE sites SET favicon_path=NULL, favicon_v=NULL WHERE id={_q(r['site_id'])} AND (favicon_path IS NOT NULL OR favicon_v IS NOT NULL);")
    return '\n'.join(lines) + ('\n' if lines else '')


def exclude_sites(db, ids, apply=False, log=print):
    """サイトを台帳で除外にする(取り下げ・除外の最初の段。R2 には触れない)。除外にすると取得しなくなり、`sql` が favicon 列を NULL にする UPDATE を出す。
    --apply のときだけ台帳に印を付ける。台帳に無い id は作らず報告する。戻り: 件数。"""
    n = 0
    for sid in ids:
        row = db.execute('SELECT 1 FROM favicons WHERE site_id=?', (sid,)).fetchone()
        log(f'{"exclude" if apply else "[dry-run] exclude"}\t{sid}' + ('' if row else '\t(台帳に無い)'))
        if apply and row:
            db.execute("UPDATE favicons SET exclude=1, exclude_reason=COALESCE(exclude_reason, 'manual') WHERE site_id=?", (sid,)); n += 1
    if apply:
        db.commit()
    return n


def delete_sites(db, store, ids, apply=False, log=print):
    """サイトのファビコンを保存先から消す(取り下げ・除外)。キーがサイト ID から決まるので他サイトの参照確認は要らない。
    --apply のときだけ消し、台帳ではそのサイトを除外にして icon_hash を空にする(`sql` が favicon 列を NULL にする UPDATE を出す)。戻り: 件数。"""
    n = 0
    for sid in ids:
        name = site_name(sid)
        row = db.execute('SELECT 1 FROM favicons WHERE site_id=?', (sid,)).fetchone()
        log(f'{"delete" if apply else "[dry-run] delete"}\t{sid}\t{store.label}:{name}' + ('' if row else '\t(台帳に無い)'))
        if not apply:
            continue
        store.delete(name)
        _unmark(db, sid, store.label)
        if row:
            db.execute("UPDATE favicons SET exclude=1, exclude_reason='deleted', icon_hash=NULL, status='excluded' WHERE site_id=?", (sid,))
        db.commit(); n += 1
    return n


def make_store(a):
    return R2Store(remote=a.r2_remote, verify=a.r2_verify) if a.store == 'r2' else LocalDirStore(a.store_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('import', help='対象サイトを台帳へ'); p.add_argument('--recrawl', help='再巡回台帳(var/recrawl.sqlite)')
    p.add_argument('--sites', nargs='*', default=[], help='[{id,url}] の JSON')
    p = sub.add_parser('run', help='期限が来たサイトを取得(--apply が無ければ計画だけ。ネットワーク・書き込みなし)')
    p.add_argument('--limit', type=int, default=50); p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--store', choices=['local', 'r2'], default='local', help='保存先(既定 local)。r2 は --apply と併せたときだけ wrangler を呼ぶ。R2 は既定でローカル模擬')
    p.add_argument('--r2-verify', action='store_true', help='--store r2 で、台帳にある画像が R2 に在るかを 1 件ずつ確かめる(遅い)')
    p.add_argument('--r2-remote', action='store_true', help='--store r2 で本番/リモートの R2 に書く(明示したときだけ)')
    p = sub.add_parser('exclude', help='サイトを台帳で除外にする(取り下げ・除外の最初の段。--apply が無ければ計画だけ。R2 には触れない。次に `sql` で D1 の favicon 列を NULL にする)')
    p.add_argument('--site', action='append', required=True, help='サイト id(複数回指定可)'); p.add_argument('--apply', action='store_true')
    p = sub.add_parser('delete', help='サイトのファビコンを保存先から消し、台帳で除外にする(取り下げ・除外。--apply が無ければ計画だけ。--r2-remote を明示したときだけ本番)')
    p.add_argument('--site', action='append', required=True, help='サイト id(複数回指定可)'); p.add_argument('--apply', action='store_true')
    p.add_argument('--store', choices=['local', 'r2'], default='r2'); p.add_argument('--store-dir', default=str(DEFAULT_STORE)); p.add_argument('--r2-verify', action='store_true'); p.add_argument('--r2-remote', action='store_true')
    p.add_argument('--d1-done', action='store_true', help='R2 の画像を消す(--apply)前に、`sql` の favicon 列 NULL を D1 へ適用済みであることの確認(D1 が先、R2 が後。逆だと一覧が一瞬 404 になる)')
    p = sub.add_parser('sql', help='sites.favicon_path / favicon_v 向けの UPDATE を出す(既定は R2 本番に置いた画像だけ)'); p.add_argument('--out'); p.add_argument('--since', help='changed_at がこれ以降のものだけ(ISO)')
    p.add_argument('--store', choices=STORE_LABELS, default='r2-remote', help='この保存先に置いた印のある画像だけ出す(既定 r2-remote。local は従来どおり)')
    sub.add_parser('status', help='台帳の集計')
    a = ap.parse_args(argv)
    if a.cmd == 'run' and a.apply:
        try:
            import PIL  # noqa: F401
        except ImportError:
            print('Pillow が無い。uv run --with pillow python3 scripts/favicon/favicon.py ... で起動する', file=sys.stderr)
            return 3
    db = connect(a.db) if a.cmd == 'import' or (a.cmd == 'run' and a.apply) or (a.cmd in ('delete', 'exclude') and a.apply) else open_readonly(a.db)
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
            store = make_store(a)
            rows = due_sites(db, now, a.limit, store) if db else []
            legacy = bool(db) and not _has_marks_table(db)
            print(f'[dry-run] 取得対象 {len(rows)} 件(--apply で実行。実サイトにアクセスする。保存先 {store.label}' + ('。旧台帳: 保存先の印を判定できないので件数は印なしの期限だけ' if legacy else '') + ')')
            for r in rows[:20]:
                print(f"  {r['site_id']}\t{r['url']}")
            return 0
        print('done ' + str(run(db, make_store(a), now, a.limit)))
    elif a.cmd == 'exclude':
        if db is None:
            print('台帳がない'); return 0
        print(f'excluded={exclude_sites(db, a.site, a.apply)}')
    elif a.cmd == 'delete':
        if db is None:
            print('台帳がない'); return 0
        if a.apply and a.store == 'r2' and not a.d1_done:
            print('R2 から消す前に、`exclude --site <id> --apply` → `sql` の出力(除外サイトの favicon 列 NULL)を D1 へ適用する。済んでいたら --d1-done を付けて再実行する(D1 が先、R2 が後)', file=sys.stderr)
            return 2
        print(f'deleted={delete_sites(db, make_store(a), a.site, a.apply)}')
    elif a.cmd == 'sql':
        if db and not _has_marks_table(db):
            print('旧台帳: 保存先の印の表が無いので --store の絞り込みは空になる(run --apply か import で書き込み可で開くと表が足される)', file=sys.stderr)
        text = sql_updates(db, a.since, a.store) if db else ''
        if a.out:
            Path(a.out).write_text(text, encoding='utf-8')
            print(f'wrote {a.out} ({text.count(chr(10))} 行)')
        else:
            sys.stdout.write(text)
    else:
        for r in (db.execute('SELECT status, COUNT(*) n FROM favicons GROUP BY status ORDER BY n DESC') if db else []):
            print(f"{r['status'] or 'pending'}\t{r['n']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
