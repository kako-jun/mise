"""サムネイルの撮影・縮小・保存(Issue #130 第1段)。手元で実行する。LLM なし、本番 D1 には書かない。R2 へは --store r2 を明示したときだけ(既定は --local の模擬)。

ファビコン(scripts/favicon/favicon.py)と同じ流儀: 台帳(SQLite, 既定 var/thumbnails.sqlite)に 1 サイト 1 行で状態を持ち、
幅 320px の WebP をサイト固定のキー(<sha256(site_id)先頭32桁>.webp。撮り直しは上書き・1 サイト 1 枚)で保存先(既定: ローカル var/thumbnails/)に置く。`sql` が sites.thumb_path / thumb_lqip /
thumb_captured_at 向けの UPDATE を出す(適用は人が行う)。撮影状態は D1 に持たない(D1 には表示に要るものだけ)。

役割分担: robots.txt(fetcher.RobotsCache。Crawl-delay を尊重)・ホスト別の待ち・台帳・1 日の上限・画像の変換は Python。
**ブラウザの全通信は netfilter.FilterProxy(検査付きフィルタリングプロキシ。宛先の名前解決・公開アドレスの検査・ポート・受信量の上限)を通し、
サンドボックス(bwrap。ネットワークなし・認証情報が見えない・書き込みは一時ディレクトリだけ)の中で動かす。**
ブラウザの中で守ること(遷移先 URL の検査・ダイアログ/ダウンロード拒否・noindex)は scripts/thumbnail/capture.mjs。
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
import shutil
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
sys.path.insert(0, str(Path(__file__).parent))
import netfilter  # noqa: E402  (アドレス判定の表と検査付きプロキシ)

CAPTURE_JS = Path(__file__).with_name('capture.mjs')
DEFAULT_DB = ROOT / 'var' / 'thumbnails.sqlite'
DEFAULT_STORE = ROOT / 'var' / 'thumbnails'
R2_PREFIX = 'thumb/'          # 配信ルート /thumb/<k>.webp のキー(<k> = sha256(site_id) 先頭 32 桁。サイト固定)
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
MAX_URL_CHECKS = 4            # 同一サイト内の未検査 URL への遷移(www・https 化・パスの違い)を、robots を確かめてやり直す回数
MAX_CRAWL_DELAY = 30          # Crawl-delay を尊重する上限(秒)。fetcher 側の上限(10 秒)がこれより小さければそちら
DEFAULT_PER_DAY = 3000        # 1 日の上限(撮影の試行数。成功・失敗とも数える)
DEFAULT_CONCURRENCY = 2
STORE_LABELS = ('local', 'r2-local', 'r2-remote')
# 再試行しても同じ結果になる失敗(月次に戻す)。それ以外は一時的として RETRY_DAYS 後(連続 RETRY_LIMIT 回で月次)
PERMANENT_ERRORS = {'ssrf_blocked', 'robots_disallowed', 'bad_url', 'bad_port', 'cross-site-redirect', 'blank', 'bad_image', 'dns', 'resource-limit', 'upstream_dns_unpinned'}
PERMANENT_PREFIXES = ('x-robots-tag:', 'meta-robots:', 'http-4')
TRANSIENT_HTTP = {'http-408', 'http-429'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs (
  site_id TEXT PRIMARY KEY, url TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',          -- pending(未撮影) / ok(撮れた) / failed(最後の試行が失敗。前回の画像があれば thumb_hash に残る)
  last_error TEXT, http_status INTEGER, final_url TEXT,
  thumb_hash TEXT, lqip TEXT, bytes INTEGER,        -- thumb_hash は画像の内容ハッシュ(変更検知用。保存先のキーはサイト固定で別)
  obscured INTEGER NOT NULL DEFAULT 0, obscured_ratio REAL,    -- cookie 同意バナー・全面モーダルの疑い(撮り直しはしない。画面側の判断材料)
  exclude INTEGER NOT NULL DEFAULT 0, exclude_reason TEXT,     -- 手動の除外(成人向けの属性が sites に無い間の代わり)。撮らず、sql にも出さない
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  notes TEXT,                                                  -- 診断の注記(例: 明示フラグで許した、名前解決が上流になった接続: upstream_dns_unpinned: <host>)
  captured_at TEXT, changed_at TEXT, last_checked_at TEXT, next_check_at TEXT);
-- どの保存先に、どのキー(<sha256(site_id)先頭32桁>.webp)で、どの内容(thumbs.thumb_hash)を置いたかの印。保存先ごとに別(local / r2-local / r2-remote)。
-- キーはサイト固定で撮り直しは上書きなので、印は「そのキーに置いた最後の内容」を 1 つだけ持つ。
CREATE TABLE IF NOT EXISTS thumb_stored (name TEXT NOT NULL, store TEXT NOT NULL, content TEXT, PRIMARY KEY (name, store));
-- 1 日の試行数(UTC の日付ごと)。--per-day の上限判定に使う。
CREATE TABLE IF NOT EXISTS day_counts (day TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
"""


class ThumbError(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}'); self.code = code; self.detail = detail


# ---- 保存先(ファビコンの IconStore を共用) ----
# サムネイルのキーは**サイト固定**(thumb/<sha256(site_id) 先頭 32 桁>.webp)。撮り直しは同じキーへの上書きで、古い画像は残らない
# (1 サイト 1 枚・世代管理なし。サムネイルは他サイトと同じ画像にならないので内容ハッシュによる共有・重複排除はしない。ファビコンも #163 で同じ規則にそろえた)。

class LocalDirStore(favicon.LocalDirStore):
    """ローカルのディレクトリ。同じ名前は上書きする(撮り直し)。"""
    def put(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(self.root / name)
        return True

    def delete(self, name):
        f = self.root / name
        if f.is_file():
            f.unlink(); return True
        return False


class R2Store(favicon.R2Store):
    """thumb/<キー>.webp へ置く。--local(模擬)が既定、--remote は remote=True のときだけ(favicon.R2Store と同じ)。put は上書き。"""
    def _key(self, name):
        return f'{self.bucket}/{R2_PREFIX}{name}'

    def delete(self, name):
        """R2 から 1 本消す(サイト ID からキーが決まるので、他サイトの参照確認は要らない)。"""
        rc = self.runner(['r2', 'object', 'delete', self._key(name)] + self._flag())
        if rc != 0:
            raise OSError(f'wrangler r2 object delete が失敗(終了コード {rc})')
        self.known.discard(name)
        return True


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


def content_hash(webp):
    """画像の内容ハッシュ(変更検知用)。保存先のキーとは別。"""
    return hashlib.sha256(webp).hexdigest()[:32]


def thumb_key(site_id):
    """保存先・配信のキー(拡張子なし 32 桁 16 進)。サイト ID から決まる(sha256 の先頭 32 桁)。"""
    return hashlib.sha256(str(site_id).encode('utf-8')).hexdigest()[:32]


def thumb_name(site_id):
    """sites.thumb_path の値・R2 の thumb/ 以下の名前。<キー>.webp"""
    return thumb_key(site_id) + '.webp'


# ---- 撮影(Node の作業者を呼ぶ) ----

def node_path():
    """実際の node 実行ファイル(volta 等のシムではなく本体。サンドボックスの中ではシムが使えない)。"""
    r = subprocess.run(['node', '-p', 'process.execPath'], capture_output=True, text=True, timeout=20)
    return r.stdout.strip() or shutil.which('node')


def chromium_path():
    env = os.environ.get('CHROMIUM_PATH')
    if env:
        return env
    base = Path.home() / '.cache' / 'ms-playwright'
    for d in sorted(base.glob('chromium_headless_shell-*'), reverse=True):
        for c in sorted(d.glob('*/chrome-headless-shell')):
            return str(c)
    return None


def playwright_dir():
    d = os.environ.get('PLAYWRIGHT_DIR')
    if d:
        return d
    c = ROOT / 'node_modules' / 'playwright'
    return str(c) if c.exists() else None


def _systemd_scope_ok(limit=512 * 1024 * 1024):
    """systemd-run --user --scope が使えて、メモリ上限が実際に効く(memory コントローラが委譲されている)か。cgroup v2 の memory.max を読んで確かめる。"""
    try:
        r = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={limit}', '--', 'sh', '-c', 'cat /proc/self/cgroup'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    line = next((l for l in r.stdout.splitlines() if l.startswith('0::')), None)
    if not line:
        return False
    try:
        return (Path('/sys/fs/cgroup') / line[3:].lstrip('/') / 'memory.max').read_text().strip() == str(limit)
    except OSError:
        return False


def job_tmp_base():
    """撮影用の一時ディレクトリの置き場。Unix ソケットのパス(108 字まで)に収まる短さにする(TMPDIR が長い環境で AF_UNIX path too long にならないように)。"""
    import tempfile
    base = tempfile.gettempdir()
    return base if len(base) <= 60 else '/tmp'


class NodeCapturer:
    """scripts/thumbnail/capture.mjs を 1 サイトにつき 1 プロセスで呼ぶ(毎回まっさらなブラウザ)。呼び出し: (url, allow_urls) -> (rec dict, png bytes|None)。

    - ブラウザの全通信は、この呼び出しごとに立てる FilterProxy(netfilter.py)を通す。
    - sandbox=True(既定): bwrap の中で動かす。ネットワークなし(--unshare-net。外へ出る経路はフィルタへの Unix ソケットだけ)、ルートは読み取り専用、
      /home・/root・/mnt・/media・/run・/tmp は空の tmpfs で隠す(認証情報 ~/.config・~/.ssh・~/.npmrc 等が見えない)、書き込みは撮影用の一時ディレクトリだけ、
      環境変数は空にして必要なものだけ渡す、親が死んだらブラウザも死ぬ(--die-with-parent。PID・IPC・UTS を分離、--new-session)。
    - メモリ・CPU の上限は systemd-run --user --scope(使えない環境では上限なしで続行し、警告を出す)。
    - sandbox=False は bwrap なし・TCP のプロキシ(テスト・GPU で固まらず信頼できる環境用)。
    - 受信量・接続数の上限をフィルタが超えたら、ブラウザごと止めて resource-limit にする。"""
    def __init__(self, sandbox=True, timeout=CAPTURE_TIMEOUT, memory_max='2G', cpu_quota='150%', proxy_kwargs=None, on_proxy=None, log=print, limits=True, allow_unpinned_connect=False, allow_plain_upstream=False, probe_target=('1.1.1.1', 443)):
        self.allow_unpinned_connect = allow_unpinned_connect; self.allow_plain_upstream = allow_plain_upstream; self.probe_target = probe_target
        self.allow_unpinned = allow_unpinned_connect or allow_plain_upstream   # 名前解決が上流になった(固定できなかった)撮影を保存してよいか
        self.sandbox = sandbox; self.limits = limits; self.timeout = timeout; self.memory_max = memory_max; self.cpu_quota = cpu_quota
        self.proxy_kwargs = dict(proxy_kwargs or {}, allow_unpinned_connect=allow_unpinned_connect, allow_plain_upstream=allow_plain_upstream); self.on_proxy = on_proxy; self.log = log
        self.node = node_path(); self.chromium = chromium_path(); self.playwright = playwright_dir()
        self.scope = None

    def check_ready(self):
        """起動前の確認。メモリ・CPU の上限が使えないときは、黙って続けず例外にする(`--no-limits` で明示したときだけ上限なしで進む)。"""
        if self.limits and self.scope is None:
            self.scope = _systemd_scope_ok()
        if self.limits and not self.scope:
            raise RuntimeError('systemd-run --user --scope でメモリ・CPU の上限を掛けられない(memory コントローラが委譲されていない等)。上限なしで撮るなら --no-limits を付ける')

    def check_upstream(self):
        """上流プロキシがあるなら、IP 宛て CONNECT を受け付けるかをジョブの開始時に 1 回だけ、公開 IP リテラルへのプローブ(応答コードだけ。データは送らない)で確かめる。
        受け付けない上流では例外で止める(名前の CONNECT に落とさない)。許すのは --allow-unpinned-upstream を明示したときだけ。"""
        up = self.proxy_kwargs.get('upstream')
        up = netfilter.upstream_from_env() if up is None else up
        ok, detail = netfilter.probe_upstream(up, self.probe_target)
        self.log(f'上流プロキシのプローブ: {detail}' + (' (上流なし)' if detail == 'no upstream' else ''))
        if not ok and not self.allow_unpinned_connect:
            raise RuntimeError(f'上流プロキシが IP 宛ての CONNECT を受け付けない({detail})。名前の CONNECT に落とすと名前解決が上流になり、IP を固定できない。許すなら --allow-unpinned-upstream を明示する')

    def _limits(self):
        self.check_ready()
        return ['systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={self.memory_max}', '-p', 'MemorySwapMax=0', '-p', f'CPUQuota={self.cpu_quota}', '-p', 'TasksMax=1024', '--'] if self.limits else []

    def _bwrap(self, d):
        binds = []
        if self.node:
            binds.append(str(Path(self.node).resolve().parent.parent))
        binds.append(str(CAPTURE_JS.parent))
        if self.playwright:
            binds.append(str(Path(self.playwright).resolve().parent))
        if self.chromium:
            binds.append(str(Path(self.chromium).resolve().parent))
        cmd = ['bwrap', '--die-with-parent', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net', '--new-session',
               '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
        for t in ('/tmp', '/var/tmp', '/run', '/home', '/root', '/mnt', '/media', '/opt', '/srv', '/sys'):
            if Path(t).exists():
                cmd += ['--tmpfs', t]
        for b in dict.fromkeys(binds):
            cmd += ['--ro-bind', b, b]
        if Path('/sys/devices/system/cpu').exists():
            cmd += ['--ro-bind', '/sys/devices/system/cpu', '/sys/devices/system/cpu']   # /sys は空にして、Chromium が CPU 数を読む所だけ見せる(ホストのネットワーク IF 名などを見せない)
        cmd += ['--bind', d, d, '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', d, '--setenv', 'TMPDIR', d, '--setenv', 'LANG', 'C.UTF-8']
        if self.playwright:
            cmd += ['--setenv', 'PLAYWRIGHT_DIR', self.playwright]
        if self.chromium:
            cmd += ['--setenv', 'CHROMIUM_PATH', self.chromium]
        return cmd

    def command(self, d, url, allow_urls, proxy):
        shot = str(Path(d) / 'shot.png')
        node = [self.node or 'node', str(CAPTURE_JS), '--url', url, '--out', shot, '--allow-urls', ','.join(allow_urls)]
        node += ['--proxy-unix', str(Path(d) / 'p.sock')] if self.sandbox else ['--proxy', proxy.url]
        return self._limits() + (self._bwrap(d) + ['--'] + node if self.sandbox else node)

    def __call__(self, url, allow_urls):
        import tempfile
        import threading
        with tempfile.TemporaryDirectory(prefix='mise-thumb-', dir=job_tmp_base()) as d:
            tripped = threading.Event()
            proxy = netfilter.FilterProxy(unix_path=str(Path(d) / 'p.sock') if self.sandbox else None, on_limit=tripped.set, **self.proxy_kwargs).start()
            env = dict(os.environ, TMPDIR=d)
            if not self.sandbox:
                env.pop('NODE_OPTIONS', None)
                for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
                    env.pop(k, None)   # ブラウザは環境の上流プロキシを直接使わない(フィルタが連鎖する)
            if self.playwright:
                env['PLAYWRIGHT_DIR'] = self.playwright
            if self.chromium:
                env['CHROMIUM_PATH'] = self.chromium
            p = subprocess.Popen(self.command(d, url, allow_urls, proxy), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
            out = ''
            reason = None
            deadline = time.monotonic() + self.timeout

            def kill():
                try:
                    os.killpg(p.pid, signal.SIGKILL)   # 自分が起動したプロセスグループだけ
                except ProcessLookupError:
                    pass
            try:
                waiter = threading.Thread(target=lambda: None)
                res = {}
                def comm():
                    res['out'], res['err'] = p.communicate()
                t = threading.Thread(target=comm, daemon=True); t.start()
                while t.is_alive():
                    t.join(0.3)
                    if tripped.is_set():
                        reason = 'resource-limit'; kill(); break
                    if time.monotonic() > deadline:
                        reason = 'timeout'; kill(); break
                t.join(10)
                out = res.get('out') or ''
            except BaseException:
                kill(); p.wait(); proxy.stop(); raise   # Ctrl-C でもブラウザを残さない
            finally:
                proxy.stop()
            stats = {'over_limit': proxy.over_limit, 'blocked': list(proxy.blocked), 'bytes': proxy.bytes, 'conns': proxy.conns, 'forwarded': list(proxy.forwarded), 'unpinned': list(proxy.unpinned)}
            if self.on_proxy:
                self.on_proxy(stats)
            if proxy.over_limit:
                return {'ok': False, 'reason': 'resource-limit', 'proxy': stats}, None
            if reason:
                return {'ok': False, 'reason': reason, 'proxy': stats}, None
            rec = None
            for line in reversed(out.strip().splitlines()):
                try:
                    rec = json.loads(line); break
                except ValueError:
                    continue
            if not isinstance(rec, dict):
                return {'ok': False, 'reason': 'error:no-result', 'proxy': stats}, None
            rec['proxy'] = stats
            shot = Path(d) / 'shot.png'
            if rec.get('ok') and shot.is_file():
                return rec, shot.read_bytes()
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
    try:
        db.execute('ALTER TABLE thumbs ADD COLUMN notes TEXT')   # 旧い台帳に足す
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


def set_exclude(db, ids, flag=True, reason='manual', missing=None):
    """除外の手動フラグ。台帳に無い id は作らず数えない(missing のリストに集める)。戻り: 更新した件数。"""
    n = 0
    for sid in ids:
        k = db.execute('UPDATE thumbs SET exclude=?, exclude_reason=? WHERE site_id=?', (1 if flag else 0, reason if flag else None, sid)).rowcount
        n += k
        if not k and missing is not None:
            missing.append(sid)
    db.commit()
    return n


def uploaded_sites(db, ids):
    """ids のうち、R2(r2-local / r2-remote)に画像を置いた印があるサイト。除外しても R2 の /thumb/<k>.webp は公開で取れたまま残るので delete が要る。"""
    out = []
    for sid in ids:
        try:
            if db.execute("SELECT 1 FROM thumb_stored WHERE name=? AND store IN ('r2-local','r2-remote')", (thumb_name(sid),)).fetchone():
                out.append(sid)
        except sqlite3.OperationalError:
            pass
    return out


def _marked(db, name, label, content=None):
    """この保存先のこのキーに、(content が与えられればその内容を)置いた印があるか。"""
    try:
        r = db.execute('SELECT content FROM thumb_stored WHERE name=? AND store=?', (name, label)).fetchone()
    except sqlite3.OperationalError:   # 読み取り専用で開いた古い/空の台帳
        return False
    return r is not None and (content is None or r['content'] == content)


def _mark(db, name, label, content):
    db.execute('INSERT INTO thumb_stored(name, store, content) VALUES(?,?,?) ON CONFLICT(name, store) DO UPDATE SET content=excluded.content', (name, label, content))


def _unmark(db, name, label):
    db.execute('DELETE FROM thumb_stored WHERE name=? AND store=?', (name, label))


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


def error_code(error):
    """last_error から失敗コードだけ取り出す('robots_unavailable: http_503 proxy=yes' → 'robots_unavailable')。"""
    e = (error or '').split(' [', 1)[0]   # 末尾の注記(' [upstream_dns_unpinned: …]')を除く
    return e if e.startswith(PERMANENT_PREFIXES) else e.split(': ', 1)[0]


def retry_days(error, failures):
    """失敗後に次を試すまでの日数。恒久的な失敗は月次、一時的な失敗は 3 日後(連続 RETRY_LIMIT 回で月次)。"""
    e = error_code(error)
    if e in TRANSIENT_HTTP:
        pass
    elif e in PERMANENT_ERRORS or e.startswith(PERMANENT_PREFIXES):
        return OK_INTERVAL_DAYS
    return OK_INTERVAL_DAYS if failures >= RETRY_LIMIT else RETRY_DAYS


def record(db, store, row, res, now):
    """結果を台帳と保存先に反映する。内容ハッシュが同じなら保存先に書かない。戻り: 'changed'|'same'|'error'"""
    sid = row['site_id']; t = now_iso(now)
    if res['status'] == 'ok':
        name = thumb_name(sid); content = content_hash(res['webp'])
        out = 'same' if row['thumb_hash'] == content else 'changed'
        if not (store.trust_ledger and _marked(db, name, store.label, content)):   # R2 は同じ内容の印があれば put しない(wrangler 起動を省く)
            store.put(name, res['webp'])   # 同じキーへの上書き(古い画像は残らない)
        _mark(db, name, store.label, content)   # put が例外なら印は付かない
        db.execute("UPDATE thumbs SET state='ok', last_error=NULL, http_status=?, final_url=?, thumb_hash=?, lqip=?, bytes=?, obscured=?, obscured_ratio=?, "
                   "consecutive_failures=0, notes=?, captured_at=?, changed_at=COALESCE(?, changed_at), last_checked_at=?, next_check_at=? WHERE site_id=?",
                   (res.get('http_status'), res.get('final_url'), content, res['lqip'], len(res['webp']), 1 if res.get('obscured') else 0, res.get('obscured_ratio'),
                    res.get('notes'), t, t if out == 'changed' else None, t, _plus(now, OK_INTERVAL_DAYS), sid))
    else:
        err = res.get('error') or 'error'
        fails = row['consecutive_failures'] + 1
        # 失敗しても前回の画像(thumb_hash)は消さない。一時的な不具合で絵が消えないことを優先する(消すのは取り下げ申請 #29)
        shown = err + (f" [{res['notes']}]" if res.get('notes') else '')   # 上流が名前解決した事実を last_error にも残す
        db.execute("UPDATE thumbs SET state='failed', last_error=?, http_status=?, final_url=?, consecutive_failures=?, notes=?, last_checked_at=?, next_check_at=? WHERE site_id=?",
                   (shown, res.get('http_status'), res.get('final_url'), fails, res.get('notes'), t, _plus(now, retry_days(err, fails)), sid))
        out = 'error'
    db.commit()
    return out


def count_attempt(db, now):
    db.execute('INSERT INTO day_counts(day, n) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET n = n + 1', (now_iso(now)[:10],))
    db.commit()


# ---- 1 サイト分 ----

def host_key(host):
    return (host or '').lower().removeprefix('www.')


class Gate:
    """robots.txt 遵守(Crawl-delay を尊重)・ホスト別の待ち・同一サイトの直列化。fetcher.RobotsCache を共用する。
    ロックは「サイト(www. を除いたホスト名)」ごと。別サイトの撮影は同時に進む(同時 2 が事実上の直列にならない)。"""
    def __init__(self, sleep=time.sleep, clock=time.monotonic, host_wait=HOST_WAIT):
        self.session = favicon.Session(sleep=sleep, clock=clock, host_wait=host_wait)
        self.site_locks = {}
        self.guard = threading.Lock()

    def site_lock(self, host):
        with self.guard:
            return self.site_locks.setdefault(host_key(host), threading.Lock())

    def robots(self, url):
        """この URL を撮ってよければ (None, crawl_delay)、だめなら (失敗コード, 0)。失敗コードには診断の詳細を付ける。"""
        parts = urllib.parse.urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        try:
            st, ok, delay = self.session.robots.check(origin, (parts.path or '/') + (f'?{parts.query}' if parts.query else ''))
        except fetcher.FetchError as e:
            return f'{e.code}: {str(e.detail)[:80]}', 0
        if st == 'unavailable':
            via = 'yes' if fetcher._uses_proxy(parts.scheme) else 'no'
            return f'robots_unavailable: {self.session.robots.detail.get(origin) or "unknown"} upstream_proxy={via}', 0
        return (None, min(delay or 0, MAX_CRAWL_DELAY)) if ok else ('robots_disallowed', 0)

    def wait(self, host, delay=0):
        self.session.wait(host, delay)

    def touch(self, host):
        self.session.last[host] = self.session.clock()


def precheck(url):
    """撮る前の事前判定(ネットワークは名前解決のみ)。公開アドレスだけを撮る(フィルタとは別に、ここでも判定する二重防御。判定表は netfilter 1 か所)。戻り: 失敗コードか None。"""
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
        netfilter.resolve_public(parts.hostname)
    except netfilter.Blocked as b:
        return {'non_public': 'ssrf_blocked', 'dns_failed': 'dns', 'bad_host': 'bad_url'}.get(b.reason, b.reason)
    return None


def refine(rec, host):
    """ブラウザの失敗理由を、フィルタが止めた記録で言い換える(自分の宛先が内部アドレスなら ssrf_blocked)。"""
    reason = rec.get('reason') or 'error'
    pr = rec.get('proxy') or {}
    if pr.get('over_limit'):
        return 'resource-limit'
    for dest, why in pr.get('blocked', []):   # フィルタが自分のページ(主フレーム)を拒否した: ブラウザには 403 の応答か接続エラーとして見える
        if why == 'plain_http_upstream' and host_key(dest.split(':')[0]) == host_key(host):
            return 'upstream_dns_unpinned'   # 上流あり環境の平文 HTTP のサイト(IP 固定できないので既定では撮らない)
    if reason.startswith('error:') and 'net::' in reason:
        for dest, why in pr.get('blocked', []):
            if host_key(dest.split(':')[0].split(' -> ')[0]) == host_key(host) and why in ('non_public', 'dns_failed'):
                return 'ssrf_blocked' if why == 'non_public' else 'dns'
    return reason


def attempt(url, gate, capturer):
    """1 サイトを 1 回撮る。戻り dict: status(ok|error), error, webp, lqip, http_status, final_url, obscured, obscured_ratio。
    同一サイト内で未検査の URL へ遷移する(www・https 化・パスの違い)ときは、遷移先の完全な URL の robots・公開アドレスを確かめて、
    最大 MAX_URL_CHECKS 回やり直す(未検査の URL へのリクエストは送る前に止まるので、サイトの本体を読むのは最後の 1 回だけ。途中の遷移応答は繰り返す)。"""
    host = urllib.parse.urlsplit(url).hostname or ''
    with gate.site_lock(host):
        bad = precheck(url)
        if bad:
            return {'status': 'error', 'error': bad}
        allowed = []
        target = url
        for hop in range(MAX_URL_CHECKS + 1):
            if hop:
                bad = precheck(target)
                if bad:
                    return {'status': 'error', 'error': bad}
            bad, delay = gate.robots(target)
            if bad:
                return {'status': 'error', 'error': bad}
            allowed.append(target)
            thost = urllib.parse.urlsplit(target).hostname
            gate.wait(thost, delay)
            rec, png = capturer(url, allowed)
            gate.touch(thost)
            reason = rec.get('reason') or ''
            if not rec.get('ok') and reason.startswith('redirect-unchecked:') and hop < MAX_URL_CHECKS:
                nxt = rec.get('new_url') or reason.split(':', 1)[1]
                nh = urllib.parse.urlsplit(nxt).hostname or ''
                if nxt not in allowed and fetcher.same_site(nh, host):
                    target = nxt
                    continue
            break
        un = (rec.get('proxy') or {}).get('unpinned') or []
        notes = ('upstream_dns_unpinned: ' + ','.join(sorted(set(un)))) if un else None   # 明示フラグで許した、名前解決が上流になった接続(IP を固定できなかった)
        if un and not getattr(capturer, 'allow_unpinned', False):
            return {'status': 'error', 'error': 'upstream_dns_unpinned', 'notes': notes}   # 名前解決が上流になった撮影は、画像を保存もアップロードもしない
        base = {'notes': notes, 'http_status': rec.get('http_status'), 'final_url': rec.get('final_url'), 'obscured': (rec.get('obscured_ratio') or 0) > 0.5, 'obscured_ratio': rec.get('obscured_ratio')}
        if (rec.get('proxy') or {}).get('over_limit'):
            return dict(base, status='error', error='resource-limit')
        if not rec.get('ok') or png is None:
            return dict(base, status='error', error=refine(rec, host))
        try:
            webp, lqip = make_thumb(png)
        except ThumbError as e:
            return dict(base, status='error', error=e.code)
        return dict(base, status='ok', webp=webp, lqip=lqip)


# ---- 実行 ----

def run(db, store, capturer, now=None, limit=50, per_day=DEFAULT_PER_DAY, concurrency=DEFAULT_CONCURRENCY, gate=None, log=print):
    """期限が来たサイトを撮る。Ctrl-C などの中断では、起動したブラウザ・フィルタを NodeCapturer が確実に止める。"""
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
            log(f"{row['site_id']}\t{out}\t{res.get('error') or ''}\t{len(res['webp']) if res.get('webp') else ''}" + (f"\t{res['notes']}" if res.get('notes') else ''))
    return counts


def upload(db, store, store_dir, apply=False, log=print):
    """ローカルに撮ってある画像のうち、この保存先のキーに「現在の内容」を置いた印の無いものを置く(サイトには一切アクセスしない)。戻り: 件数。"""
    rows = db.execute('SELECT site_id, thumb_hash FROM thumbs WHERE thumb_hash IS NOT NULL AND exclude=0 ORDER BY site_id').fetchall()
    todo = []
    for r in rows:
        name = thumb_name(r['site_id']); f = Path(store_dir) / name
        if not _marked(db, name, store.label, r['thumb_hash']) and f.is_file() and content_hash(f.read_bytes()) == r['thumb_hash']:
            todo.append((r['site_id'], name, r['thumb_hash']))
    log(f'{"upload" if apply else "[dry-run] upload"} {len(todo)} 件(台帳の画像 {len(rows)} 件のうち、{store.label} に現在の内容の印が無くローカルにある分)')
    if not apply:
        return len(todo)
    n = 0
    for sid, name, content in todo:
        try:
            store.put(name, (Path(store_dir) / name).read_bytes())
        except OSError as e:
            log(f'{sid}\tstore_failed: {type(e).__name__}')
            continue
        _mark(db, name, store.label, content); db.commit(); n += 1
    return n


def delete_sites(db, store, ids, store_dir=None, apply=False, log=print):
    """サイトのサムネイルを保存先から消す(取り下げ・除外)。キーがサイト ID から決まるので他サイトの参照確認は要らない。
    --apply のときだけ消し、台帳ではそのサイトを除外にして thumb_hash・lqip を空にする(`sql` が thumb 列を NULL にする UPDATE を出す)。戻り: 件数。"""
    n = 0
    for sid in ids:
        name = thumb_name(sid)
        row = db.execute('SELECT 1 FROM thumbs WHERE site_id=?', (sid,)).fetchone()
        log(f'{"delete" if apply else "[dry-run] delete"}\t{sid}\t{store.label}:{name}' + ('' if row else '\t(台帳に無い)'))
        if not apply:
            continue
        store.delete(name)
        _unmark(db, name, store.label)
        if row:
            db.execute("UPDATE thumbs SET exclude=1, exclude_reason='deleted', thumb_hash=NULL, lqip=NULL, bytes=NULL, state='pending' WHERE site_id=?", (sid,))
        db.commit(); n += 1
    return n


def _q(s):
    return 'NULL' if s is None else "'" + str(s).replace("'", "''") + "'"


def sql_updates(db, since=None, store=None):
    """sites.thumb_path / thumb_lqip / thumb_captured_at へ入れる UPDATE。
    thumb_path はサイト固定(<sha256(site_id) 先頭32桁>.webp)で撮り直しでも変わらないので、**冪等判定は thumb_captured_at の変化**(同じ撮影時刻なら書かない)。
    store(ラベル名)を渡すと、その保存先のそのキーに現在の内容を置いた印のある画像だけを出す(D1 が指す画像が配信元に無い状態を作らない)。
    除外のサイトは、thumb 列を NULL にする UPDATE を出す(`AND thumb_path IS NOT NULL` なので何度流しても安全。除外が唯一の成人向けフィルタ)。"""
    q = 'SELECT site_id, thumb_hash, lqip, captured_at FROM thumbs WHERE thumb_hash IS NOT NULL AND exclude=0 AND captured_at IS NOT NULL'
    args = ()
    if since:
        q += ' AND captured_at >= ?'; args += (since,)
    rows = db.execute(q + ' ORDER BY site_id', args).fetchall()
    if store:
        rows = [r for r in rows if _marked(db, thumb_name(r['site_id']), store, r['thumb_hash'])]
    lines = [f"UPDATE sites SET thumb_path={_q(thumb_name(r['site_id']))}, thumb_lqip={_q(r['lqip'])}, thumb_captured_at={_q(r['captured_at'])} "
             f"WHERE id={_q(r['site_id'])} AND thumb_captured_at IS NOT {_q(r['captured_at'])};" for r in rows]
    for r in db.execute('SELECT site_id FROM thumbs WHERE exclude=1 ORDER BY site_id'):
        lines.append(f"UPDATE sites SET thumb_path=NULL, thumb_lqip=NULL, thumb_captured_at=NULL WHERE id={_q(r['site_id'])} AND thumb_path IS NOT NULL;")
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
    p.add_argument('--delete-uploaded', action='store_true', help='アップロード済みのサイトを除外したとき、R2 から消す delete のコマンド(dry-run)を案内する')
    p = sub.add_parser('run', help='期限が来たサイトを撮る(--apply が無ければ計画だけ。ネットワーク・書き込みなし)')
    p.add_argument('--limit', type=int, default=50); p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--per-day', type=int, default=DEFAULT_PER_DAY, help=f'1 日(UTC)の試行数の上限(既定 {DEFAULT_PER_DAY})')
    p.add_argument('--concurrency', type=int, default=DEFAULT_CONCURRENCY, help=f'同時に撮る数(既定 {DEFAULT_CONCURRENCY}。同じホストは常に直列)')
    p.add_argument('--allow-unpinned-upstream', action='store_true', help='上流プロキシが IP 宛て CONNECT を受け付けなくても、名前の CONNECT で撮る(名前解決が上流になる。記録に残る)')
    p.add_argument('--allow-plain-http-upstream', action='store_true', help='上流プロキシ経由で平文 HTTP を撮る(IP 固定できず名前解決が上流になる。既定は撮らない)')
    p.add_argument('--upstream-probe', default='1.1.1.1:443', help='上流プロキシが IP 宛て CONNECT を受け付けるかを確かめる公開 IP(応答コードだけ確認。既定 1.1.1.1:443)')
    p.add_argument('--no-limits', action='store_true', help='メモリ・CPU の上限(systemd-run --user --scope)なしで撮る。上限を掛けられない環境でだけ明示する')
    p.add_argument('--no-bwrap', action='store_true', help='サンドボックス(bwrap)を使わない。信頼できる環境・テスト用(既定は使う)')
    p.add_argument('--store', choices=['local', 'r2'], default='local', help='保存先(既定 local)。r2 は --apply と併せたときだけ wrangler を呼ぶ。R2 は既定でローカル模擬')
    p.add_argument('--r2-verify', action='store_true', help='--store r2 で、台帳にある画像が R2 に在るかを 1 件ずつ確かめる(遅い)')
    p.add_argument('--r2-remote', action='store_true', help='--store r2 で本番/リモートの R2 に書く(明示したときだけ)')
    p = sub.add_parser('upload', help='ローカルに撮ってある画像を R2 へ置く(サイトにはアクセスしない。--apply が無ければ計画だけ)')
    p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--r2-verify', action='store_true'); p.add_argument('--r2-remote', action='store_true')
    p = sub.add_parser('delete', help='サイトのサムネイルを保存先から消し、台帳で除外にする(取り下げ・除外。--apply が無ければ計画だけ。--r2-remote を明示したときだけ本番)')
    p.add_argument('--site', action='append', required=True, help='サイト id(複数回指定可)'); p.add_argument('--apply', action='store_true'); p.add_argument('--store-dir', default=str(DEFAULT_STORE))
    p.add_argument('--store', choices=['local', 'r2'], default='r2'); p.add_argument('--r2-verify', action='store_true'); p.add_argument('--r2-remote', action='store_true')
    p.add_argument('--d1-done', action='store_true', help='R2 の画像を消す(--apply)前に、`sql` の thumb 列 NULL を D1 へ適用済みであることの確認(D1 が先、R2 が後。逆だと一覧が一瞬 404 になる)')
    p = sub.add_parser('sql', help='sites.thumb_path / thumb_lqip / thumb_captured_at 向けの UPDATE を出す(既定は R2 本番に置いた画像だけ)')
    p.add_argument('--out'); p.add_argument('--since', help='captured_at(撮影時刻)がこれ以降のものだけ(ISO)')
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
            print('bwrap が見つからない(サンドボックスが要る)。信頼できる環境でだけ --no-bwrap を付ける', file=sys.stderr)
            return 3
    db = connect(a.db) if a.cmd == 'import' or (a.cmd == 'delete' and a.apply) or (a.cmd == 'run' and a.apply) or (a.cmd == 'upload' and a.apply) else open_readonly(a.db)
    now = datetime.datetime.now(datetime.timezone.utc)
    if a.cmd == 'import':
        n = 0
        for f in a.sites:
            n += import_sites(db, load_sites(f))
        print(f'imported={n}')
        for spec, flag, label in ((a.exclude, True, 'excluded'), (a.unexclude, False, 'unexcluded')):
            if spec:
                missing = []
                print(f'{label}={set_exclude(db, parse_ids(spec), flag, missing=missing)}')
                if missing:
                    print(f'warning: 台帳に無い id(先に --sites で取り込む): {", ".join(missing)}', file=sys.stderr)
                ids = [i for i in parse_ids(spec) if i not in missing]
                up = uploaded_sites(db, ids) if flag else []
                if up:
                    print(f'warning: R2 にアップロード済みのサイトを除外した。除外だけでは R2 の /thumb/<k>.webp が公開で取れたまま残る: {", ".join(up)}\n'
                          '  手順(この順): ① 除外(済) → ② `sql` を出して D1 の thumb 列を NULL にする → ③ `delete --site <id> --apply --d1-done`(R2 から消す。本番は --r2-remote)。D1 が先、R2 が後', file=sys.stderr)
                    if a.delete_uploaded:
                        remote = [i for i in up if _marked(db, thumb_name(i), 'r2-remote')]; local = [i for i in up if i not in remote]
                        print('  次の dry-run を確認してから --apply --d1-done を付ける:', file=sys.stderr)
                        for ids_, flag_ in ((remote, ' --r2-remote'), (local, '')):   # r2-local にだけ上げたサイトに --r2-remote は付けない
                            if ids_:
                                print('  uv run --with pillow python3 scripts/thumbnail/thumbnail.py delete ' + ' '.join(f'--site {i}' for i in ids_) + flag_, file=sys.stderr)
    elif a.cmd == 'run':
        if not a.apply:
            rows = due_sites(db, now, min(a.limit, remaining_today(db, now, a.per_day))) if db else []
            left = remaining_today(db, now, a.per_day) if db else a.per_day
            print(f'[dry-run] 撮影対象 {len(rows)} 件(--apply で実行。実サイトにアクセスする。保存先 {make_store(a).label}。今日の残り枠 {left}/{a.per_day}、同時 {a.concurrency})')
            for r in rows[:20]:
                print(f"  {r['site_id']}\t{r['url']}")
            return 0
        try:
            ph, _, pp = a.upstream_probe.rpartition(':')
            cap = NodeCapturer(sandbox=not a.no_bwrap, limits=not a.no_limits, allow_unpinned_connect=a.allow_unpinned_upstream, allow_plain_upstream=a.allow_plain_http_upstream, probe_target=(ph, int(pp)))
            cap.check_ready(); cap.check_upstream()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 3
        print('done ' + str(run(db, make_store(a), cap, now, a.limit, a.per_day, a.concurrency)))
    elif a.cmd == 'upload':
        if db is None:
            print('台帳がない'); return 0
        st = R2Store(remote=a.r2_remote, verify=a.r2_verify)
        print(f'uploaded={upload(db, st, a.store_dir, a.apply)}')
    elif a.cmd == 'delete':
        if db is None:
            print('台帳がない'); return 0
        if a.apply and a.store == 'r2' and not a.d1_done:
            print('R2 から消す前に、`sql` の出力(除外サイトの thumb 列 NULL)を D1 へ適用する。済んでいたら --d1-done を付けて再実行する(D1 が先、R2 が後)', file=sys.stderr)
            return 2
        print(f'deleted={delete_sites(db, make_store(a), a.site, a.store_dir, a.apply)}')
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
