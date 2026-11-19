"""サムネイルの撮影・縮小・保存(Issue #130 第1段)。手元で実行する。LLM なし、本番 D1 には書かない。R2 へは --store r2 を明示したときだけ(既定は --local の模擬)。

ファビコン(scripts/favicon/favicon.py)と同じ流儀: 台帳(SQLite, 既定 var/thumbnails.sqlite)に 1 サイト 1 行で状態を持ち、
幅 320px の WebP を内容ハッシュ名(<hash>.webp)で保存先(既定: ローカル var/thumbnails/)に置く。`sql` が sites.thumb_path / thumb_lqip /
thumb_captured_at 向けの UPDATE を出す(適用は人が行う)。撮影状態は D1 に持たない(D1 には表示に要るものだけ)。

役割分担: robots.txt(fetcher.RobotsCache)・ホスト別の待ち・SSRF の事前判定・台帳・1 日の上限・画像の変換は Python、
ブラウザの中で守ること(リダイレクト先の検査・内部 IP・ダイアログ/ダウンロード拒否・リソース上限)は scripts/thumbnail/capture.mjs。
取れない/失敗時は何も出さない(代替画像は作らない)。成人向けの属性は sites に無いので、手動の除外フラグ(exclude)だけで除外する。
画像の検証と縮小に Pillow を使う: `uv run --with pillow python3 scripts/thumbnail/thumbnail.py ...`
"""
import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
sys.path.insert(0, str(ROOT / 'scripts' / 'favicon'))
import fetcher  # noqa: E402
import favicon  # noqa: E402  (保存先の抽象 IconStore / LocalDirStore / R2Store と Session を共用する)

CAPTURE_JS = Path(__file__).with_name('capture.mjs')
DEFAULT_DB = ROOT / 'var' / 'thumbnails.sqlite'
DEFAULT_STORE = ROOT / 'var' / 'thumbnails'
R2_PREFIX = 'thumb/'          # 配信ルート /thumb/<hash>.webp のキー
VIEWPORT = (1280, 800)        # PC 画面の最初の画面(16:10)
THUMB_W = 320
THUMB_H = round(THUMB_W * VIEWPORT[1] / VIEWPORT[0])   # 200
WEBP_QUALITY = 40
LQIP_MAX_CHARS = 180          # 約 130 文字が目安。データ URI の接頭辞込みの上限
LQIP_STEPS = ((16, 10, 30), (16, 10, 10), (12, 8, 10), (8, 5, 10))   # (幅, 高さ, 画質): 上限に収まるまで小さくする
BLANK_STDDEV = 1.5            # 全体の画素のばらつきがこれ未満なら白紙/無地とみなして不採用
OK_INTERVAL_DAYS = 30         # 月次
RETRY_DAYS = 3                # 一時的な失敗の再試行
RETRY_LIMIT = 3               # 連続でこの回数失敗したら月次の間隔に戻す
HOST_WAIT = 2.0               # 同一ホストへのリクエスト間隔(秒)
CAPTURE_TIMEOUT = 90          # 1 サイトの撮影全体の強制終了(秒)
MAX_ORIGIN_HOPS = 2           # 同一サイト内で origin が変わる(www・https 化)遷移を追う回数
DEFAULT_PER_DAY = 3000        # 1 日の上限(撮影の試行数。成功・失敗とも数える)
DEFAULT_CONCURRENCY = 2
STORE_LABELS = ('local', 'r2-local', 'r2-remote')
# 再試行しても同じ結果になる失敗(月次に戻す)。それ以外は一時的として RETRY_DAYS 後(連続 RETRY_LIMIT 回で月次)
PERMANENT_ERRORS = {'ssrf_blocked', 'robots_disallowed', 'bad_url', 'bad_port', 'unsafe-url', 'cross-site-redirect', 'blank', 'bad_image', 'dns'}
PERMANENT_PREFIXES = ('x-robots-tag:', 'meta-robots:', 'http-4')
TRANSIENT_HTTP = {'http-408', 'http-429'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs (
  site_id TEXT PRIMARY KEY, url TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',          -- pending(未撮影) / ok(撮れた) / failed(最後の試行が失敗。前回の画像があれば thumb_hash に残る)
  last_error TEXT, http_status INTEGER, final_url TEXT,
  thumb_hash TEXT, lqip TEXT, bytes INTEGER,
  obscured INTEGER NOT NULL DEFAULT 0, obscured_ratio REAL,    -- cookie 同意バナー・全面モーダルの疑い(撮り直しはしない。画面側の判断材料)
  exclude INTEGER NOT NULL DEFAULT 0, exclude_reason TEXT,     -- 手動の除外(成人向けの属性が sites に無い間の代わり)。撮らず、sql にも出さない
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  captured_at TEXT, changed_at TEXT, last_checked_at TEXT, next_check_at TEXT);
-- どの保存先にどの画像を置いたかの印。保存先ごとに別(local / r2-local / r2-remote)。
CREATE TABLE IF NOT EXISTS thumb_stored (name TEXT NOT NULL, store TEXT NOT NULL, PRIMARY KEY (name, store));
-- 1 日の試行数(UTC の日付ごと)。--per-day の上限判定に使う。
CREATE TABLE IF NOT EXISTS day_counts (day TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
"""


class ThumbError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}'); self.code = code; self.detail = detail


# ---- 保存先(ファビコンの IconStore を共用) ----

LocalDirStore = favicon.LocalDirStore


class R2Store(favicon.R2Store):
    """thumb/<hash>.webp へ置く。--local(模擬)が既定、--remote は remote=True のときだけ(favicon.R2Store と同じ)。"""
    def _key(self, name):
        return f'{self.bucket}/{R2_PREFIX}{name}'


# ---- 画像 ----

def make_thumb(png):
    """撮影した PNG(1280x800)から、幅 320 の WebP と極小プレースホルダを作る。戻り: (webp bytes, lqip データURI)。白紙・壊れた画像は ThumbError。"""
    from PIL import Image, ImageStat
    try:
        im = Image.open(io.BytesIO(png)); im.load()
        im = im.convert('RGB')
    except Exception as e:
        raise ThumbError('bad_image', type(e).__name__)
    if min(im.size) < 100:
        raise ThumbError('bad_image', f'{im.size}')
    if max(ImageStat.Stat(im).stddev) < BLANK_STDDEV:
        raise ThumbError('blank')
    small = im.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, 'WEBP', quality=WEBP_QUALITY, method=6)
    return buf.getvalue(), make_lqip(im)


def make_lqip(im):
    """data:image/webp;base64,... の極小プレースホルダ。LQIP_MAX_CHARS に収まるまで小さく・粗くする。外部依存なし。"""
    from PIL import Image
    out = ''
    for w, h, q in LQIP_STEPS:
        b = io.BytesIO()
        im.resize((w, h), Image.LANCZOS).save(b, 'WEBP', quality=q, method=6)
        out = 'data:image/webp;base64,' + base64.b64encode(b.getvalue()).decode()
        if len(out) <= LQIP_MAX_CHARS:
            return out
    return out


def thumb_name(webp):
    return hashlib.sha256(webp).hexdigest()[:32] + '.webp'


# ---- 撮影(Node の作業者を呼ぶ) ----

class NodeCapturer:
    """scripts/thumbnail/capture.mjs を 1 サイトにつき 1 プロセスで呼ぶ(毎回まっさらなブラウザ)。呼び出し: (url, allow_origins) -> (rec dict, png bytes|None)。
    headless chromium は GPU デバイスを開く所(drm_open)で固まることがあるので、既定で bwrap で /dev/dri を隠す。"""
    def __init__(self, bwrap=True, timeout=CAPTURE_TIMEOUT, node='node'):
        self.bwrap = bwrap; self.timeout = timeout; self.node = node

    def __call__(self, url, allow_origins):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / 'shot.png'
            cmd = [self.node, str(CAPTURE_JS), '--url', url, '--out', str(png), '--allow-origins', ','.join(sorted(allow_origins))]
            if self.bwrap:
                cmd = ['bwrap', '--dev-bind', '/', '/', '--tmpfs', '/dev/dri'] + cmd
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                out, _err = p.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)   # 自分が起動したプロセスグループだけ
                except ProcessLookupError:
                    pass
                p.communicate()
                return {'ok': False, 'reason': 'timeout'}, None
            rec = None
            for line in reversed((out or '').strip().splitlines()):
                try:
                    rec = json.loads(line); break
                except ValueError:
                    continue
            if not isinstance(rec, dict):
                return {'ok': False, 'reason': 'error:no-result'}, None
            if rec.get('ok') and png.is_file():
                return rec, png.read_bytes()
            return dict(rec, ok=False), None


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


def open_readonly(path):
    """読み取り専用で開く(台帳もディレクトリも作らない)。無ければ None。"""
    if not Path(path).is_file():
        return None
    db = sqlite3.connect('file:' + urllib.parse.quote(os.path.abspath(path)) + '?mode=ro', uri=True); db.row_factory = sqlite3.Row
    return db


def import_sites(db, rows):
    """rows: [(site_id, url)]。URL だけ更新し、状態・除外は保つ。"""
    for sid, url in rows:
        db.execute('INSERT INTO thumbs(site_id,url) VALUES(?,?) ON CONFLICT(site_id) DO UPDATE SET url=excluded.url', (sid, url))
    db.commit()
    return len(rows)


def parse_ids(spec):
    """--exclude の値: JSON の ["id",...] / [{"id":...}] を書いたファイルのパス、またはカンマ区切りの id。"""
    p = Path(spec)
    if p.is_file():
        data = json.loads(p.read_text(encoding='utf-8'))
        return [r['id'] if isinstance(r, dict) else r for r in data if (r.get('id') if isinstance(r, dict) else r)]
    return [s.strip() for s in spec.split(',') if s.strip()]


def set_exclude(db, ids, flag=True, reason='manual'):
    """除外の手動フラグ。台帳に無い id は作らず数えない。戻り: 更新した件数。"""
    n = 0
    for sid in ids:
        n += db.execute('UPDATE thumbs SET exclude=?, exclude_reason=? WHERE site_id=?', (1 if flag else 0, reason if flag else None, sid)).rowcount
    db.commit()
    return n


def _marked(db, name, label):
    try:
        return db.execute('SELECT 1 FROM thumb_stored WHERE name=? AND store=?', (name, label)).fetchone() is not None
    except sqlite3.OperationalError:   # 読み取り専用で開いた古い/空の台帳
        return False


def _mark(db, name, label):
    db.execute('INSERT OR IGNORE INTO thumb_stored(name, store) VALUES(?,?)', (name, label))


def due_sites(db, now, limit):
    """期限が来た(または未撮影の)サイト。除外は含めない。未撮影を先、次に期限の古い順。"""
    return db.execute(
        'SELECT * FROM thumbs WHERE exclude=0 AND (next_check_at IS NULL OR next_check_at <= ?) '
        'ORDER BY next_check_at IS NOT NULL, next_check_at, site_id LIMIT ?', (now_iso(now), max(0, limit))).fetchall()


def used_today(db, now):
    try:
        r = db.execute('SELECT n FROM day_counts WHERE day=?', (now_iso(now)[:10],)).fetchone()
    except sqlite3.OperationalError:
        return 0
    return r['n'] if r else 0


def remaining_today(db, now, per_day):
    return max(0, per_day - used_today(db, now))


def _plus(now, days):
    return now_iso(now + datetime.timedelta(days=days))


def retry_days(error, failures):
    """失敗後に次を試すまでの日数。恒久的な失敗は月次、一時的な失敗は 3 日後(連続 RETRY_LIMIT 回で月次)。"""
    e = error or ''
    if e in TRANSIENT_HTTP:
        pass
    elif e in PERMANENT_ERRORS or e.startswith(PERMANENT_PREFIXES):
        return OK_INTERVAL_DAYS
    return OK_INTERVAL_DAYS if failures >= RETRY_LIMIT else RETRY_DAYS


def record(db, store, row, res, now):
    """結果を台帳と保存先に反映する。内容ハッシュが同じなら保存先に書かない。戻り: 'changed'|'same'|'error'"""
    sid = row['site_id']; t = now_iso(now)
    if res['status'] == 'ok':
        name = thumb_name(res['webp'])
        out = 'same' if row['thumb_hash'] == name else 'changed'
        if not (store.trust_ledger and _marked(db, name, store.label)):   # R2 は印があれば put しない(wrangler 起動を省く)
            store.put(name, res['webp'])
        _mark(db, name, store.label)   # put が例外なら印は付かない
        db.execute("UPDATE thumbs SET state='ok', last_error=NULL, http_status=?, final_url=?, thumb_hash=?, lqip=?, bytes=?, obscured=?, obscured_ratio=?, "
                   "consecutive_failures=0, captured_at=?, changed_at=COALESCE(?, changed_at), last_checked_at=?, next_check_at=? WHERE site_id=?",
                   (res.get('http_status'), res.get('final_url'), name, res['lqip'], len(res['webp']), 1 if res.get('obscured') else 0, res.get('obscured_ratio'),
                    t, t if out == 'changed' else None, t, _plus(now, OK_INTERVAL_DAYS), sid))
    else:
        err = res.get('error') or 'error'
        fails = row['consecutive_failures'] + 1
        # 失敗しても前回の画像(thumb_hash)は消さない。一時的な不具合で絵が消えないことを優先する(消すのは取り下げ申請 #29)
        db.execute("UPDATE thumbs SET state='failed', last_error=?, http_status=?, final_url=?, consecutive_failures=?, last_checked_at=?, next_check_at=? WHERE site_id=?",
                   (err, res.get('http_status'), res.get('final_url'), fails, t, _plus(now, retry_days(err, fails)), sid))
        out = 'error'
    db.commit()
    return out


def count_attempt(db, now):
    db.execute('INSERT INTO day_counts(day, n) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET n = n + 1', (now_iso(now)[:10],))
    db.commit()


# ---- 1 サイト分 ----

class Gate:
    """robots.txt 遵守・ホスト別の待ち・同一ホストの直列化。fetcher.RobotsCache を共用する(スレッド間は 1 本のロックで守る)。"""
    def __init__(self, sleep=time.sleep, clock=time.monotonic, host_wait=HOST_WAIT):
        self.session = favicon.Session(sleep=sleep, clock=clock, host_wait=host_wait)
        self.lock = threading.Lock()
        self.host_locks = {}
        self.host_locks_guard = threading.Lock()

    def host_lock(self, host):
        with self.host_locks_guard:
            return self.host_locks.setdefault(host, threading.Lock())

    def robots(self, url):
        """この URL を撮ってよければ None、だめなら失敗コード。"""
        parts = urllib.parse.urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        try:
            with self.lock:
                st, ok, delay = self.session.robots.check(origin, (parts.path or '/') + (f'?{parts.query}' if parts.query else ''))
        except fetcher.FetchError as e:
            return e.code
        if st == 'unavailable':
            return 'robots_unavailable'
        return None if ok else 'robots_disallowed'

    def wait(self, host, delay=0):
        with self.lock:
            self.session.wait(host, delay)

    def touch(self, host):
        with self.lock:
            self.session.last[host] = self.session.clock()


def precheck(url):
    """撮る前の事前判定(ネットワークは名前解決のみ)。戻り: 失敗コードか None。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return 'bad_url'
    try:
        port = parts.port
    except ValueError:
        return 'bad_url'
    if port not in fetcher.ALLOWED_PORTS:
        return 'bad_port'
    try:
        fetcher.check_public_host(parts.hostname)
    except fetcher.FetchError as e:
        return e.code
    return None


def attempt(url, gate, capturer):
    """1 サイトを 1 回撮る。戻り dict: status(ok|error), error, webp, lqip, http_status, final_url, obscured, obscured_ratio。
    同一サイト内で origin が変わる(www・https 化)ときは、遷移先の robots を確かめて最大 MAX_ORIGIN_HOPS 回やり直す
    (最初の試行は遷移先へのリクエストを送る前に止まるので、サイトの本体を読んだのは最後の 1 回だけ)。"""
    host = urllib.parse.urlsplit(url).hostname or ''
    with gate.host_lock(host):
        bad = precheck(url)
        if bad:
            return {'status': 'error', 'error': bad}
        allowed = set()
        target = url
        for hop in range(MAX_ORIGIN_HOPS + 1):
            parts = urllib.parse.urlsplit(target)
            origin = f'{parts.scheme}://{parts.netloc}'
            if origin not in allowed:
                bad = precheck(target) if hop else None
                bad = bad or gate.robots(target)
                if bad:
                    return {'status': 'error', 'error': bad}
                allowed.add(origin)
            gate.wait(parts.hostname)
            rec, png = capturer(url, allowed)
            gate.touch(parts.hostname)
            reason = rec.get('reason') or ''
            if not rec.get('ok') and reason.startswith('origin-changed:') and hop < MAX_ORIGIN_HOPS:
                new = urllib.parse.urlsplit(reason.split(':', 1)[1])
                if new.hostname and fetcher.same_site(new.hostname, host):
                    target = f'{new.scheme}://{new.netloc}/'
                    continue
            break
        base = {'http_status': rec.get('http_status'), 'final_url': rec.get('final_url'), 'obscured': (rec.get('obscured_ratio') or 0) > 0.5, 'obscured_ratio': rec.get('obscured_ratio')}
        if not rec.get('ok') or png is None:
            return dict(base, status='error', error=reason or 'error')
        try:
            webp, lqip = make_thumb(png)
        except ThumbError as e:
            return dict(base, status='error', error=e.code)
        return dict(base, status='ok', webp=webp, lqip=lqip)


# ---- 実行 ----

def run(db, store, capturer, now=None, limit=50, per_day=DEFAULT_PER_DAY, concurrency=DEFAULT_CONCURRENCY, gate=None, log=print):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    gate = gate or Gate()
    budget = min(limit, remaining_today(db, now, per_day))
    rows = due_sites(db, now, budget)
    counts = {}
    if not rows:
        return counts

    def work(row):
        try:
            return row, attempt(row['url'], gate, capturer)
        except ImportError:
            raise   # Pillow が無い等は 1 サイトの失敗ではない。全体を止める
        except Exception as e:   # 1 サイトの想定外の例外で全体を止めない
            return row, {'status': 'error', 'error': type(e).__name__}

    for row in rows:
        count_attempt(db, now)   # 試行は始める前に数える(途中で止まっても上限を超えない)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(work, r) for r in rows]
        for f in as_completed(futures):
            row, res = f.result()
            try:
                out = record(db, store, row, res, now)
            except OSError as e:   # 保存先に書けない(ディスク満杯・wrangler 失敗等): このサイトだけ失敗として続行
                res = {'status': 'error', 'error': f'store_failed: {type(e).__name__}'}
                out = record(db, store, row, res, now)
            counts[out] = counts.get(out, 0) + 1
            log(f"{row['site_id']}\t{out}\t{res.get('error') or ''}\t{len(res['webp']) if res.get('webp') else ''}")
    return counts


def upload(db, store, store_dir, apply=False, log=print):
    """ローカルに撮ってある画像のうち、この保存先に印の無いものを置く(サイトには一切アクセスしない)。戻り: 件数。"""
    names = [r['thumb_hash'] for r in db.execute('SELECT DISTINCT thumb_hash FROM thumbs WHERE thumb_hash IS NOT NULL AND exclude=0 ORDER BY thumb_hash')]
    todo = [n for n in names if not _marked(db, n, store.label) and (Path(store_dir) / n).is_file()]
    log(f'{"upload" if apply else "[dry-run] upload"} {len(todo)} 件(台帳の画像 {len(names)} 件のうち、{store.label} に印が無くローカルにある分)')
    if not apply:
        return len(todo)
    n = 0
    for name in todo:
        try:
            store.put(name, (Path(store_dir) / name).read_bytes())
        except OSError as e:
            log(f'{name}\tstore_failed: {type(e).__name__}')
            continue
        _mark(db, name, store.label); db.commit(); n += 1
    return n


def _q(s):
    return 'NULL' if s is None else "'" + str(s).replace("'", "''") + "'"


def sql_updates(db, since=None, store=None):
    """sites.thumb_path / thumb_lqip / thumb_captured_at へ入れる UPDATE。値が同じ(thumb_path が変わらない)なら書かない。
    store(ラベル名)を渡すと、その保存先に置いた印のある画像だけを出す(D1 が指す画像が配信元に無い状態を作らない)。除外のサイトは出さない。"""
    q = 'SELECT site_id, thumb_hash, lqip, captured_at FROM thumbs WHERE thumb_hash IS NOT NULL AND exclude=0'
    args = ()
    if store:
        q += ' AND thumb_hash IN (SELECT name FROM thumb_stored WHERE store=?)'; args += (store,)
    if since:
        q += ' AND changed_at >= ?'; args += (since,)
    try:
        rows = db.execute(q + ' ORDER BY site_id', args).fetchall()
    except sqlite3.OperationalError:   # 印の表が無い台帳を読み取り専用で開いた
        if not store:
            raise
        rows = []
    lines = [f"UPDATE sites SET thumb_path={_q(r['thumb_hash'])}, thumb_lqip={_q(r['lqip'])}, thumb_captured_at={_q(r['captured_at'])} "
             f"WHERE id={_q(r['site_id'])} AND thumb_path IS NOT {_q(r['thumb_hash'])};" for r in rows]
    return '\n'.join(lines) + ('\n' if lines else '')


def make_store(a):
    return R2Store(remote=a.r2_remote, verify=a.r2_verify) if a.store == 'r2' else LocalDirStore(a.store_dir)


def load_sites(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return [(r['id'], r['url']) for r in data if r.get('id') and r.get('url')]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('import', help='対象サイトを台帳へ(と、手動の除外)')
    p.add_argument('--sites', nargs='*', default=[], help='[{id,url}] の JSON')
    p.add_argument('--exclude', help='撮らないサイト。id のカンマ区切り、または id の JSON ファイル。成人向けの属性が sites に無い間の手動の除外')
    p.add_argument('--unexclude', help='除外を解く(同じ形式)')
    p = sub.add_parser('run', help='期限が来たサイトを撮る(--apply が無ければ計画だけ。ネットワーク・書き込みなし)')
    p.add_argument('--limit', type=int, default=50); p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--per-day', type=int, default=DEFAULT_PER_DAY, help=f'1 日(UTC)の試行数の上限(既定 {DEFAULT_PER_DAY})')
    p.add_argument('--concurrency', type=int, default=DEFAULT_CONCURRENCY, help=f'同時に撮る数(既定 {DEFAULT_CONCURRENCY}。同じホストは常に直列)')
    p.add_argument('--no-bwrap', action='store_true', help='GPU で固まらない環境で bwrap を使わない')
    p.add_argument('--store', choices=['local', 'r2'], default='local', help='保存先(既定 local)。r2 は --apply と併せたときだけ wrangler を呼ぶ。R2 は既定でローカル模擬')
    p.add_argument('--r2-verify', action='store_true', help='--store r2 で、台帳にある画像が R2 に在るかを 1 件ずつ確かめる(遅い)')
    p.add_argument('--r2-remote', action='store_true', help='--store r2 で本番/リモートの R2 に書く(明示したときだけ)')
    p = sub.add_parser('upload', help='ローカルに撮ってある画像を R2 へ置く(サイトにはアクセスしない。--apply が無ければ計画だけ)')
    p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--r2-verify', action='store_true'); p.add_argument('--r2-remote', action='store_true')
    p = sub.add_parser('sql', help='sites.thumb_path / thumb_lqip / thumb_captured_at 向けの UPDATE を出す(既定は R2 本番に置いた画像だけ)')
    p.add_argument('--out'); p.add_argument('--since', help='changed_at がこれ以降のものだけ(ISO)')
    p.add_argument('--store', choices=STORE_LABELS, default='r2-remote', help='この保存先に置いた印のある画像だけ出す(既定 r2-remote)')
    sub.add_parser('status', help='台帳の集計')
    a = ap.parse_args(argv)
    if a.cmd == 'run' and a.apply:
        try:
            import PIL  # noqa: F401
        except ImportError:
            print('Pillow が無い。uv run --with pillow python3 scripts/thumbnail/thumbnail.py ... で起動する', file=sys.stderr)
            return 3
        if not a.no_bwrap and subprocess.run(['which', 'bwrap'], stdout=subprocess.DEVNULL).returncode != 0:
            print('bwrap が見つからない。GPU で固まらない環境なら --no-bwrap を付ける', file=sys.stderr)
            return 3
    db = connect(a.db) if a.cmd == 'import' or (a.cmd == 'run' and a.apply) or (a.cmd == 'upload' and a.apply) else open_readonly(a.db)
    now = datetime.datetime.now(datetime.timezone.utc)
    if a.cmd == 'import':
        n = 0
        for f in a.sites:
            n += import_sites(db, load_sites(f))
        print(f'imported={n}')
        if a.exclude:
            print(f'excluded={set_exclude(db, parse_ids(a.exclude))}')
        if a.unexclude:
            print(f'unexcluded={set_exclude(db, parse_ids(a.unexclude), False)}')
    elif a.cmd == 'run':
        if not a.apply:
            rows = due_sites(db, now, min(a.limit, remaining_today(db, now, a.per_day))) if db else []
            left = remaining_today(db, now, a.per_day) if db else a.per_day
            print(f'[dry-run] 撮影対象 {len(rows)} 件(--apply で実行。実サイトにアクセスする。保存先 {make_store(a).label}。今日の残り枠 {left}/{a.per_day}、同時 {a.concurrency})')
            for r in rows[:20]:
                print(f"  {r['site_id']}\t{r['url']}")
            return 0
        print('done ' + str(run(db, make_store(a), NodeCapturer(bwrap=not a.no_bwrap), now, a.limit, a.per_day, a.concurrency)))
    elif a.cmd == 'upload':
        if db is None:
            print('台帳がない'); return 0
        st = R2Store(remote=a.r2_remote, verify=a.r2_verify)
        print(f'uploaded={upload(db, st, a.store_dir, a.apply)}')
    elif a.cmd == 'sql':
        text = sql_updates(db, a.since, a.store) if db else ''
        if a.out:
            Path(a.out).write_text(text, encoding='utf-8')
            print(f'wrote {a.out} ({text.count(chr(10))} 行)')
        else:
            sys.stdout.write(text)
    else:
        if db:
            for r in db.execute("SELECT CASE WHEN exclude=1 THEN 'excluded' ELSE state END AS s, COUNT(*) n FROM thumbs GROUP BY s ORDER BY n DESC"):
                print(f"{r['s']}\t{r['n']}")
            print(f"obscured\t{db.execute('SELECT COUNT(*) FROM thumbs WHERE obscured=1').fetchone()[0]}")
            print(f'today\t{used_today(db, now)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
