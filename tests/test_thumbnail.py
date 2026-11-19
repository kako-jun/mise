"""サムネイル撮影(#130)。ネットワーク・Chromium は全てモック。実行: uv run --with pillow python3 -m unittest tests.test_thumbnail"""
import contextlib
import datetime
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'thumbnail'))
import thumbnail as T
from thumbnail import fetcher
from PIL import Image

UTC = datetime.timezone.utc


def _resolve(host):
    """名前解決の模擬(ネットワークなし)。localhost は 127.0.0.1、*.invalid は引けない、それ以外の名前は公開アドレスに解決されたことにする。"""
    if host == 'localhost':
        return ['127.0.0.1']
    if host.endswith('.invalid'):
        raise OSError('no such host')
    return ['93.184.216.34']


def setUpModule():
    global _patch
    _patch = mock.patch.object(T.netfilter, '_system_resolve', _resolve); _patch.start()


def tearDownModule():
    _patch.stop()


T0 = datetime.datetime(2026, 11, 20, 3, 0, 0, tzinfo=UTC)


def png(color=None, size=(1280, 800), noisy=True):
    """撮影結果の PNG。noisy なら模様つき(白紙でない)。"""
    im = Image.new('RGB', size, color or (240, 240, 240))
    if noisy:
        for x in range(0, size[0], 40):
            for y in range(0, size[1], 40):
                im.paste(((x * 7) % 256, (y * 5) % 256, (x + y) % 256), (x, y, x + 20, y + 20))
    b = io.BytesIO(); im.save(b, 'PNG')
    return b.getvalue()


class FakeGate:
    """robots / 待ちを持たない門(ネットワークなし)。disallow に入れたホストは robots_disallowed。delays は ホスト -> Crawl-delay。"""
    def __init__(self, disallow=(), unavailable=(), delays=None):
        import threading
        self.disallow = set(disallow); self.unavailable = set(unavailable); self.delays = delays or {}; self.checked = []; self.waited = []
        self.locks = {}; self.guard = threading.Lock()

    def site_lock(self, host):
        with self.guard:
            return self.locks.setdefault(T.host_key(host), __import__('threading').Lock())

    def robots(self, url):
        host = T.urllib.parse.urlsplit(url).hostname; self.checked.append(url)
        if host in self.disallow:
            return 'robots_disallowed', 0
        if host in self.unavailable:
            return 'robots_unavailable: http_503 upstream_proxy=no', 0
        return None, self.delays.get(host, 0)

    def wait(self, host, delay=0):
        self.waited.append((host, delay))

    def touch(self, host):
        pass


def fake_capturer(results):
    """results: url -> rec(dict) か rec のリスト(呼ぶたび順に)。ok のとき撮影画像を返す。"""
    calls = []

    def cap(url, allow):
        calls.append((url, set(allow)))
        r = results[url]
        if isinstance(r, list):
            r = r.pop(0)
        r = dict(r)
        return r, (png() if r.get('ok') else None)
    cap.calls = calls
    return cap


OK = {'ok': True, 'http_status': 200, 'final_url': 'https://a.example/', 'obscured_ratio': 0.1}


class Image_(unittest.TestCase):
    def test_make_thumb_size_and_lqip(self):
        webp, lqip = T.make_thumb(png())
        im = Image.open(io.BytesIO(webp))
        self.assertEqual((im.format, im.size), ('WEBP', (320, 200)))
        self.assertTrue(lqip.startswith('data:image/webp;base64,'))
        self.assertLessEqual(len(lqip), T.LQIP_MAX_CHARS)
        self.assertGreater(len(lqip), 60)   # 約 130 文字の目安(極端に短くはならない)
        self.assertRegex(T.content_hash(webp), r'^[0-9a-f]{32}$')

    def test_lqip_is_decodable_and_small_even_for_noisy_image(self):
        import base64
        _, lqip = T.make_thumb(png())
        im = Image.open(io.BytesIO(base64.b64decode(lqip.split(',', 1)[1])))
        self.assertEqual(im.format, 'WEBP'); self.assertLessEqual(im.size[0], 16)
        # ノイズだらけの画像でも上限に収まる(段階的に小さくする)
        rnd = Image.effect_noise((1280, 800), 100).convert('RGB'); b = io.BytesIO(); rnd.save(b, 'PNG')
        self.assertLessEqual(len(T.make_thumb(b.getvalue())[1]), T.LQIP_MAX_CHARS)

    def test_blank_and_broken_rejected(self):
        for data, code in ((png(noisy=False), 'blank'), (png((255, 255, 255), noisy=False), 'blank'), (b'not an image', 'bad_image'), (png(size=(40, 40)), 'bad_image')):
            with self.assertRaises(T.ThumbError) as c:
                T.make_thumb(data)
            self.assertEqual(c.exception.code, code)

    def test_key_is_sha256_of_site_id_and_independent_of_content(self):
        import hashlib
        self.assertEqual(T.thumb_key('site-1'), hashlib.sha256(b'site-1').hexdigest()[:32])
        self.assertEqual(T.thumb_name('site-1'), T.thumb_key('site-1') + '.webp')
        self.assertRegex(T.thumb_name('日本語のid'), r'^[0-9a-f]{32}\.webp$')
        self.assertNotEqual(T.thumb_key('a'), T.thumb_key('b'))
        self.assertEqual(T.content_hash(T.make_thumb(png())[0]), T.content_hash(T.make_thumb(png())[0]))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.db = T.connect(':memory:'); self.addCleanup(self.db.close)

    def test_import_keeps_state_and_exclude(self):
        T.import_sites(self.db, [('a', 'https://a.example/'), ('b', 'https://b.example/')])
        T.set_exclude(self.db, ['a'])
        T.import_sites(self.db, [('a', 'https://a2.example/')])
        r = self.db.execute("SELECT url, exclude, exclude_reason FROM thumbs WHERE site_id='a'").fetchone()
        self.assertEqual((r['url'], r['exclude'], r['exclude_reason']), ('https://a2.example/', 1, 'manual'))

    def test_exclude_only_existing_and_reversible(self):
        T.import_sites(self.db, [('a', 'https://a.example/')])
        self.assertEqual(T.set_exclude(self.db, ['a', 'zzz']), 1)
        self.assertEqual(T.set_exclude(self.db, ['a'], False), 1)
        self.assertEqual(self.db.execute("SELECT exclude FROM thumbs").fetchone()[0], 0)

    def test_parse_ids(self):
        self.assertEqual(T.parse_ids('a, b,,c'), ['a', 'b', 'c'])
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'x.json'; f.write_text(json.dumps([{'id': 'a'}, 'b']))
            self.assertEqual(T.parse_ids(str(f)), ['a', 'b'])

    def test_due_order_new_first_then_oldest_and_excluded_skipped(self):
        T.import_sites(self.db, [(s, f'https://{s}.example/') for s in 'abcdef'])
        t = lambda days: T.now_iso(T0 + datetime.timedelta(days=days))
        for sid, nxt in (('a', t(-1)), ('b', t(-5)), ('c', t(+1)), ('d', None)):
            self.db.execute('UPDATE thumbs SET next_check_at=? WHERE site_id=?', (nxt, sid))
        T.set_exclude(self.db, ['e'])    # e は未撮影でも除外
        self.assertEqual([r['site_id'] for r in T.due_sites(self.db, T0, 10)], ['d', 'f', 'b', 'a'])   # c は期限前
        self.assertEqual([r['site_id'] for r in T.due_sites(self.db, T0, 2)], ['d', 'f'])

    def test_per_day_budget(self):
        T.import_sites(self.db, [(s, f'https://{s}.example/') for s in 'abcde'])
        gate = FakeGate(); cap = fake_capturer({f'https://{s}.example/': dict(OK) for s in 'abcde'})
        with tempfile.TemporaryDirectory() as d:
            c1 = T.run(self.db, T.LocalDirStore(d), cap, T0, limit=10, per_day=3, gate=gate, log=lambda *_: None)
            self.assertEqual(sum(c1.values()), 3)
            self.assertEqual(T.used_today(self.db, T0), 3)
            self.assertEqual(T.run(self.db, T.LocalDirStore(d), cap, T0, limit=10, per_day=3, gate=gate, log=lambda *_: None), {})   # 今日はもう撮らない
            nxt = T0 + datetime.timedelta(days=1)   # 翌日は枠が戻る(まだ未撮影の 2 件)
            c3 = T.run(self.db, T.LocalDirStore(d), cap, nxt, limit=10, per_day=3, gate=gate, log=lambda *_: None)
            self.assertEqual(sum(c3.values()), 2)

    def test_retry_days(self):
        self.assertEqual(T.retry_days('timeout', 1), T.RETRY_DAYS)
        self.assertEqual(T.retry_days('timeout', 3), T.OK_INTERVAL_DAYS)
        self.assertEqual(T.retry_days('http-503', 1), T.RETRY_DAYS)
        self.assertEqual(T.retry_days('http-429', 1), T.RETRY_DAYS)
        for e in ('robots_disallowed', 'ssrf_blocked', 'cross-site-redirect', 'http-404', 'meta-robots:noindex', 'x-robots-tag:noarchive', 'blank'):
            self.assertEqual(T.retry_days(e, 1), T.OK_INTERVAL_DAYS, e)
        self.assertEqual(T.retry_days('robots_unavailable', 1), T.RETRY_DAYS)
        self.assertEqual(T.retry_days('robots_unavailable: http_503 upstream_proxy=yes', 1), T.RETRY_DAYS)
        self.assertEqual(T.retry_days('robots_disallowed', 1), T.OK_INTERVAL_DAYS)
        self.assertEqual(T.retry_days('resource-limit', 1), T.OK_INTERVAL_DAYS)


class Attempt(unittest.TestCase):
    def test_ok(self):
        gate = FakeGate(); cap = fake_capturer({'https://a.example/': dict(OK)})
        r = T.attempt('https://a.example/', gate, cap)
        self.assertEqual(r['status'], 'ok'); self.assertTrue(r['webp']); self.assertTrue(r['lqip'])
        self.assertEqual(cap.calls, [('https://a.example/', {'https://a.example/'})])
        self.assertEqual(gate.waited, [('a.example', 0)])

    def test_crawl_delay_is_waited(self):
        gate = FakeGate(delays={'a.example': 7}); cap = fake_capturer({'https://a.example/': dict(OK)})
        T.attempt('https://a.example/', gate, cap)
        self.assertEqual(gate.waited, [('a.example', 7)])

    def test_ssrf_precheck_never_calls_browser(self):
        gate = FakeGate(); cap = fake_capturer({})
        for url in ('http://127.0.0.1/', 'http://10.1.2.3/', 'http://localhost/', 'file:///etc/passwd', 'ftp://a.example/', 'https://a.example:8443/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://x.invalid/'):
            r = T.attempt(url, gate, cap)
            self.assertEqual(r['status'], 'error', url)
            self.assertIn(r['error'], ('ssrf_blocked', 'bad_url', 'bad_port', 'dns'), url)
        self.assertEqual(cap.calls, []); self.assertEqual(gate.checked, [])

    def test_robots_disallow_and_unavailable_with_detail(self):
        cap = fake_capturer({'https://a.example/': dict(OK)})
        self.assertEqual(T.attempt('https://a.example/', FakeGate(disallow={'a.example'}), cap)['error'], 'robots_disallowed')
        e = T.attempt('https://a.example/', FakeGate(unavailable={'a.example'}), cap)['error']
        self.assertTrue(e.startswith('robots_unavailable: http_503')); self.assertEqual(T.error_code(e), 'robots_unavailable')
        self.assertEqual(cap.calls, [])

    def test_unchecked_redirect_checks_robots_of_full_url_then_retries(self):
        gate = FakeGate(); cap = fake_capturer({'https://a.example/': [
            {'ok': False, 'reason': 'redirect-unchecked:https://www.a.example/ja/top?x=1', 'new_url': 'https://www.a.example/ja/top?x=1'}, dict(OK)]})
        r = T.attempt('https://a.example/', gate, cap)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(gate.checked, ['https://a.example/', 'https://www.a.example/ja/top?x=1'])   # 新 origin の / だけでなく、実際の遷移先の全体
        self.assertEqual(cap.calls[1][1], {'https://a.example/', 'https://www.a.example/ja/top?x=1'})
        self.assertEqual([h for h, _ in gate.waited], ['a.example', 'www.a.example'])

    def test_redirect_to_disallowed_url_stops(self):
        gate = FakeGate(disallow={'www.a.example'}); cap = fake_capturer({'https://a.example/': [{'ok': False, 'reason': 'redirect-unchecked:https://www.a.example/x', 'new_url': 'https://www.a.example/x'}]})
        self.assertEqual(T.attempt('https://a.example/', gate, cap)['error'], 'robots_disallowed')
        self.assertEqual(len(cap.calls), 1)

    def test_redirect_to_other_site_or_ssrf_is_not_followed(self):
        for new in ('https://evil.example/', 'http://10.0.0.1/'):
            gate = FakeGate(); cap = fake_capturer({'https://a.example/': [{'ok': False, 'reason': 'redirect-unchecked:' + new, 'new_url': new}]})
            r = T.attempt('https://a.example/', gate, cap)
            self.assertEqual(r['status'], 'error'); self.assertEqual(len(cap.calls), 1, new)
            self.assertEqual(gate.checked, ['https://a.example/'])

    def test_redirect_checks_are_limited(self):
        gate = FakeGate(); n = [0]

        def cap(url, allow):
            n[0] += 1
            u = f'https://a.example/p{n[0]}'
            return {'ok': False, 'reason': 'redirect-unchecked:' + u, 'new_url': u}, None
        T.attempt('https://a.example/', gate, cap)
        self.assertEqual(n[0], T.MAX_URL_CHECKS + 1)

    def test_blank_page_is_failure(self):
        cap = lambda url, allow: ({'ok': True}, png(noisy=False))
        self.assertEqual(T.attempt('https://a.example/', FakeGate(), cap)['error'], 'blank')

    def test_browser_reason_passthrough_and_obscured(self):
        cap = fake_capturer({'https://a.example/': {'ok': False, 'reason': 'meta-robots:noindex', 'http_status': 200}})
        r = T.attempt('https://a.example/', FakeGate(), cap)
        self.assertEqual((r['status'], r['error']), ('error', 'meta-robots:noindex'))
        cap = fake_capturer({'https://a.example/': dict(OK, obscured_ratio=0.8)})
        self.assertTrue(T.attempt('https://a.example/', FakeGate(), cap)['obscured'])

    def test_resource_limit_and_filter_block_are_reported(self):
        cap = lambda url, allow: ({'ok': False, 'reason': 'resource-limit', 'proxy': {'over_limit': True, 'blocked': []}}, None)
        self.assertEqual(T.attempt('https://a.example/', FakeGate(), cap)['error'], 'resource-limit')
        cap = lambda url, allow: ({'ok': False, 'reason': 'error:net::ERR_TUNNEL_CONNECTION_FAILED', 'proxy': {'over_limit': False, 'blocked': [('a.example:443 -> 10.0.0.1', 'non_public')]}}, None)
        self.assertEqual(T.attempt('https://a.example/', FakeGate(), cap)['error'], 'ssrf_blocked')


class RobotsReal(unittest.TestCase):
    """Gate は fetcher.RobotsCache を再利用する(http_get をモック)。"""
    def gate(self, body, status=200):
        g = T.Gate(sleep=lambda s: None)
        patcher = mock.patch.object(fetcher, 'http_get', lambda url, max_body=0, extra=None: (status, {}, body))
        patcher.start(); self.addCleanup(patcher.stop)
        return g

    def test_disallow_all(self):
        g = self.gate(b'User-agent: *\nDisallow: /\n')
        self.assertEqual(g.robots('https://a.example/')[0], 'robots_disallowed')

    def test_own_token_disallow(self):
        g = self.gate(b'User-agent: MiseResearchBot\nDisallow: /private\n')
        self.assertEqual(g.robots('https://a.example/private/x')[0], 'robots_disallowed')
        self.assertIsNone(g.robots('https://a.example/')[0])

    def test_404_means_allowed_and_5xx_unavailable_403_disallowed(self):
        self.assertIsNone(self.gate(b'', 404).robots('https://a.example/')[0])
        self.assertTrue(self.gate(b'', 503).robots('https://b.example/')[0].startswith('robots_unavailable'))
        self.assertEqual(self.gate(b'', 403).robots('https://c.example/')[0], 'robots_disallowed')

    def test_same_host_gap_at_least_two_seconds(self):
        slept = []; now = [100.0]
        g = T.Gate(sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
        g.wait('a.example'); now[0] += 0.5; g.wait('a.example')
        self.assertAlmostEqual(slept[0], 1.5)
        self.assertGreaterEqual(T.HOST_WAIT, 2.0)

    def test_crawl_delay_is_returned_and_capped(self):
        g = self.gate(b'User-agent: *\nCrawl-delay: 5\n')
        self.assertEqual(g.robots('https://a.example/'), (None, 5.0))
        g2 = self.gate(b'User-agent: *\nCrawl-delay: 500\n')
        self.assertLessEqual(g2.robots('https://b.example/')[1], T.MAX_CRAWL_DELAY)
        slept = []; now = [100.0]
        g3 = T.Gate(sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
        g3.wait('c.example', 5); g3.wait('c.example', 5)
        self.assertAlmostEqual(slept[0], 5.0)   # Crawl-delay が 2 秒の既定より長ければそちらで待つ

    def test_unavailable_detail_records_status_and_proxy_usage(self):
        with mock.patch.dict('os.environ', {'https_proxy': 'http://proxy.invalid:8080'}):
            r = self.gate(b'', 503).robots('https://b.example/')[0]
        self.assertEqual(r, 'robots_unavailable: http_503 upstream_proxy=yes')
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(self.gate(b'', 502).robots('https://c.example/')[0], 'robots_unavailable: http_502 upstream_proxy=no')

    def test_fetch_error_detail(self):
        g = T.Gate(sleep=lambda s: None)
        with mock.patch.object(fetcher, 'http_get', side_effect=fetcher.FetchError('connect', 'curl: (7) refused')):
            self.assertEqual(g.robots('https://a.example/')[0], 'connect: curl: (7) refused')

    def test_lock_is_per_site_not_global(self):
        import threading
        g = T.Gate(sleep=lambda s: None)
        la = g.site_lock('a.example'); self.assertIs(la, g.site_lock('www.a.example')); self.assertIsNot(la, g.site_lock('b.example'))
        with la:   # a.example を握っていても b.example の robots は進む
            done = []
            t = threading.Thread(target=lambda: (g.site_lock('b.example').acquire(), done.append(1))); t.start(); t.join(2)
            self.assertEqual(done, [1])


def png_of(seed):
    """seed ごとに内容が違う撮影結果(同じサイトの撮り直しで画像が変わる場合の模擬)。"""
    im = Image.new('RGB', (1280, 800), (seed * 37 % 256, 100, 200 - seed))
    for x in range(0, 1280, 64):
        im.paste((seed * 11 % 256, x % 256, 50), (x, 0, x + 32, 800))
    b = io.BytesIO(); im.save(b, 'PNG')
    return b.getvalue()


class StoreAndSql(unittest.TestCase):
    def setUp(self):
        self.db = T.connect(':memory:'); T.import_sites(self.db, [('a', 'https://a.example/'), ('b', 'https://b.example/')]); self.addCleanup(self.db.close)
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)

    def go(self, store, cap=None, now=T0):
        cap = cap or fake_capturer({'https://a.example/': dict(OK), 'https://b.example/': dict(OK)})
        return T.run(self.db, store, cap, now, limit=10, gate=FakeGate(), log=lambda *_: None)

    def test_one_file_per_site_keyed_by_site_id_even_for_identical_images(self):
        self.go(T.LocalDirStore(self.dir.name))
        files = sorted(f.name for f in Path(self.dir.name).glob('*.webp'))
        self.assertEqual(files, sorted([T.thumb_name('a'), T.thumb_name('b')]))   # 同じ画像でも共有しない(サイトごとに 1 枚)
        self.assertEqual({r['store'] for r in self.db.execute('SELECT store FROM thumb_stored')}, {'local'})
        self.assertEqual(self.db.execute("SELECT state FROM thumbs WHERE site_id='a'").fetchone()[0], 'ok')

    def test_recapture_overwrites_same_key_and_leaves_no_old_image(self):
        st = T.LocalDirStore(self.dir.name)
        results = {'https://a.example/': dict(OK), 'https://b.example/': dict(OK)}
        pngs = iter([png_of(1), png_of(2), png_of(3), png_of(4)])
        cap = lambda url, allow: (dict(OK), next(pngs))
        self.go(st, cap)
        before = {f.name: f.read_bytes() for f in Path(self.dir.name).glob('*.webp')}
        self.go(st, cap, T0 + datetime.timedelta(days=31))
        after = {f.name: f.read_bytes() for f in Path(self.dir.name).glob('*.webp')}
        self.assertEqual(set(before), set(after))   # 古いキーが残らない(同じ 2 本のまま)
        self.assertTrue(all(before[k] != after[k] for k in before))   # 中身は新しい画像に上書き
        r = self.db.execute("SELECT thumb_hash, captured_at, changed_at FROM thumbs WHERE site_id='a'").fetchone()
        self.assertEqual(r['thumb_hash'], T.content_hash(after[T.thumb_name('a')]))
        self.assertEqual(T.thumb_name('a'), T.thumb_name('a'))
        marks = self.db.execute("SELECT content FROM thumb_stored WHERE name=?", (T.thumb_name('a'),)).fetchall()
        self.assertEqual([m['content'] for m in marks], [r['thumb_hash']])   # 印も 1 行(最後の内容)

    def test_sql_only_r2_remote_marked_and_uses_site_key_and_captured_at(self):
        self.go(T.LocalDirStore(self.dir.name))
        self.assertEqual(T.sql_updates(self.db, store='r2-remote'), '')   # ローカルにしか無い画像は出さない
        sql = T.sql_updates(self.db, store='local')
        self.assertEqual(sql.count('UPDATE sites'), 2)
        k = T.thumb_name('a')
        self.assertIn(f"thumb_path='{k}'", sql)
        self.assertRegex(sql, r"thumb_lqip='data:image/webp;base64,[A-Za-z0-9+/=]+'")
        self.assertIn("thumb_captured_at='2026-11-20T03:00:00Z'", sql)
        self.assertIn("AND thumb_captured_at IS NOT '2026-11-20T03:00:00Z';", sql)   # 冪等判定は撮影時刻(thumb_path は撮り直しで変わらない)
        content = self.db.execute("SELECT thumb_hash FROM thumbs WHERE site_id='a'").fetchone()[0]
        T._mark(self.db, k, 'r2-remote', 'stale-content')
        self.assertEqual(T.sql_updates(self.db, store='r2-remote'), '')   # 現在の内容と違う印では出さない
        T._mark(self.db, k, 'r2-remote', content)
        self.assertEqual(T.sql_updates(self.db, store='r2-remote').count('UPDATE sites'), 1)
        self.assertEqual(T.sql_updates(self.db, since='2099-01-01T00:00:00Z', store='local'), '')

    def test_sql_idempotency_by_captured_at_in_sqlite(self):
        """撮り直しで thumb_path が同じでも、撮影時刻が変われば UPDATE が効く。同じ撮影時刻なら効かない(行を書かない)。"""
        self.go(T.LocalDirStore(self.dir.name))
        d = sqlite3.connect(':memory:')
        d.execute('CREATE TABLE sites(id TEXT PRIMARY KEY, thumb_path TEXT, thumb_lqip TEXT, thumb_captured_at TEXT)'); d.execute("INSERT INTO sites(id) VALUES('a'),('b')")
        d.executescript(T.sql_updates(self.db, store='local'))
        self.assertEqual(d.execute("SELECT thumb_path, thumb_captured_at FROM sites WHERE id='a'").fetchone(), (T.thumb_name('a'), '2026-11-20T03:00:00Z'))
        n0 = d.total_changes; d.executescript(T.sql_updates(self.db, store='local')); self.assertEqual(d.total_changes, n0)   # 同じ撮影時刻 → 書かない
        self.go(T.LocalDirStore(self.dir.name), None, T0 + datetime.timedelta(days=31))
        d.executescript(T.sql_updates(self.db, store='local'))
        self.assertEqual(d.total_changes - n0, 2)   # 撮影時刻が変わった 2 サイト分だけ
        self.assertEqual(d.execute("SELECT thumb_path, thumb_captured_at FROM sites WHERE id='a'").fetchone(), (T.thumb_name('a'), '2026-12-21T03:00:00Z'))

    def test_sql_defaults_in_cli_are_r2_remote(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = str(Path(d) / 't.sqlite'); db = T.connect(dbp); T.import_sites(db, [('a', 'https://a.example/')])
            T.run(db, T.LocalDirStore(Path(d) / 's'), fake_capturer({'https://a.example/': dict(OK)}), T0, gate=FakeGate(), log=lambda *_: None); db.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.main(['--db', dbp, 'sql'])
            self.assertEqual(out.getvalue(), '')   # 既定は r2-remote の印がある画像だけ
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.main(['--db', dbp, 'sql', '--store', 'local'])
            self.assertIn('UPDATE sites SET thumb_path=', out.getvalue())

    def test_excluded_site_gets_null_update_even_after_capture(self):
        self.go(T.LocalDirStore(self.dir.name))
        T.set_exclude(self.db, ['a'])
        sql = T.sql_updates(self.db, store='local')
        self.assertIn("UPDATE sites SET thumb_path=NULL, thumb_lqip=NULL, thumb_captured_at=NULL WHERE id='a' AND thumb_path IS NOT NULL;", sql)
        self.assertNotIn("thumb_path='" + T.thumb_name('a'), sql); self.assertIn("thumb_path='" + T.thumb_name('b'), sql)
        self.assertIn("UPDATE sites SET thumb_path=NULL", T.sql_updates(self.db, store='r2-remote'))   # ストアの印に関わらず必ず出る
        d = sqlite3.connect(':memory:')
        d.execute('CREATE TABLE sites(id TEXT PRIMARY KEY, thumb_path TEXT, thumb_lqip TEXT, thumb_captured_at TEXT)')
        d.execute("INSERT INTO sites VALUES('a','x.webp','l','t'),('b',NULL,NULL,NULL)")
        d.executescript(T.sql_updates(self.db, store='r2-remote'))
        self.assertEqual(d.execute("SELECT thumb_path, thumb_lqip, thumb_captured_at FROM sites WHERE id='a'").fetchone(), (None, None, None))

    def test_quotes_in_lqip_are_escaped(self):
        self.go(T.LocalDirStore(self.dir.name))
        self.db.execute("UPDATE thumbs SET lqip=? WHERE site_id='b'", ("data:image/webp;base64,x'y",))
        self.assertIn("x''y", T.sql_updates(self.db, store='local'))

    def test_failure_keeps_previous_image_and_schedules_retry(self):
        self.go(T.LocalDirStore(self.dir.name))
        h = self.db.execute("SELECT thumb_hash FROM thumbs WHERE site_id='a'").fetchone()[0]
        later = T0 + datetime.timedelta(days=31)
        cap = fake_capturer({'https://a.example/': {'ok': False, 'reason': 'timeout'}, 'https://b.example/': {'ok': False, 'reason': 'http-404', 'http_status': 404}})
        T.run(self.db, T.LocalDirStore(self.dir.name), cap, later, limit=10, gate=FakeGate(), log=lambda *_: None)
        a = self.db.execute("SELECT * FROM thumbs WHERE site_id='a'").fetchone(); b = self.db.execute("SELECT * FROM thumbs WHERE site_id='b'").fetchone()
        self.assertEqual((a['state'], a['last_error'], a['thumb_hash'], a['consecutive_failures']), ('failed', 'timeout', h, 1))
        self.assertEqual(a['next_check_at'], T.now_iso(later + datetime.timedelta(days=3)))
        self.assertEqual(b['next_check_at'], T.now_iso(later + datetime.timedelta(days=30)))
        self.assertIn(f"thumb_path='{T.thumb_name('a')}'", T.sql_updates(self.db, store='local'))   # 失敗しても前回の画像は消えない

    def test_recapture_same_content_does_not_touch_changed_at_or_put_to_r2(self):
        calls = []
        st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
        self.go(st)
        puts = len(calls); before = self.db.execute("SELECT changed_at FROM thumbs WHERE site_id='a'").fetchone()[0]
        later = T0 + datetime.timedelta(days=31)
        self.go(T.R2Store(runner=lambda a: (calls.append(a), 0)[1]), None, later)
        r = self.db.execute("SELECT changed_at, captured_at FROM thumbs WHERE site_id='a'").fetchone()
        self.assertEqual(r['changed_at'], before); self.assertEqual(r['captured_at'], T.now_iso(later))
        self.assertEqual(len(calls), puts)   # 内容が同じで印があるので R2 へ書かない

    def test_store_failure_is_per_site(self):
        class Boom(T.LocalDirStore):
            def put(self, name, data):
                raise OSError('disk full')
        self.go(Boom(self.dir.name))
        rows = self.db.execute('SELECT state, last_error, thumb_hash FROM thumbs').fetchall()
        self.assertTrue(all(r['state'] == 'failed' and r['last_error'].startswith('store_failed') and r['thumb_hash'] is None for r in rows))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM thumb_stored').fetchone()[0], 0)

    def test_excluded_never_captured(self):
        T.set_exclude(self.db, ['a', 'b'])
        cap = fake_capturer({})
        self.assertEqual(self.go(T.LocalDirStore(self.dir.name), cap), {}); self.assertEqual(cap.calls, [])

    def test_concurrency_runs_all_same_site_serial_different_sites_parallel(self):
        import threading, time
        T.import_sites(self.db, [(f's{i}', f'https://h{i % 2}.example/p{i}') for i in range(6)])
        active = {}; overlap = []; peak = [0]; lock = threading.Lock()

        def cap(url, allow):
            host = T.urllib.parse.urlsplit(url).hostname
            with lock:
                if active.get(host):
                    overlap.append(host)
                active[host] = True; peak[0] = max(peak[0], sum(active.values()))
            time.sleep(0.05)
            with lock:
                active[host] = False
            return dict(OK), png()
        c = T.run(self.db, T.LocalDirStore(self.dir.name), cap, T0, limit=20, concurrency=3, gate=FakeGate(), log=lambda *_: None)
        self.assertEqual(sum(c.values()), 8); self.assertEqual(overlap, []); self.assertGreaterEqual(peak[0], 2)

    def test_per_day_budget(self):
        T.import_sites(self.db, [(s, f'https://{s}.example/') for s in 'cde'])
        cap = fake_capturer({f'https://{s}.example/': dict(OK) for s in 'abcde'})
        st = T.LocalDirStore(self.dir.name)
        c1 = T.run(self.db, st, cap, T0, limit=10, per_day=3, gate=FakeGate(), log=lambda *_: None)
        self.assertEqual(sum(c1.values()), 3); self.assertEqual(T.used_today(self.db, T0), 3)
        self.assertEqual(T.run(self.db, st, cap, T0, limit=10, per_day=3, gate=FakeGate(), log=lambda *_: None), {})
        c3 = T.run(self.db, st, cap, T0 + datetime.timedelta(days=1), limit=10, per_day=3, gate=FakeGate(), log=lambda *_: None)
        self.assertEqual(sum(c3.values()), 2)


class R2(unittest.TestCase):
    def test_key_prefix_local_default_and_overwrite(self):
        calls = []
        st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
        self.assertTrue(st.put(T.thumb_name('a'), b'x'))
        a = calls[0]
        self.assertIn('mise-media/thumb/' + T.thumb_name('a'), a); self.assertIn('--local', a); self.assertNotIn('--remote', a)
        self.assertEqual(st.label, 'r2-local')
        calls.clear(); st2 = T.R2Store(remote=True, runner=lambda a: (calls.append(a), 0)[1]); st2.put(T.thumb_name('b'), b'x')
        self.assertIn('--remote', calls[0]); self.assertEqual(st2.label, 'r2-remote')

    def test_delete_uses_site_key_only_and_local_by_default(self):
        calls = []
        st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1]); st.delete(T.thumb_name('a'))
        self.assertEqual(calls[0][:3], ['r2', 'object', 'delete']); self.assertIn('mise-media/thumb/' + T.thumb_name('a'), calls[0]); self.assertIn('--local', calls[0])

    def test_upload_from_local_dir_dry_run_default_and_reupload_after_recapture(self):
        with tempfile.TemporaryDirectory() as d:
            db = T.connect(':memory:'); T.import_sites(db, [('a', 'https://a.example/')])
            pngs = iter([png_of(1), png_of(2)])
            cap = lambda url, allow: (dict(OK), next(pngs))
            T.run(db, T.LocalDirStore(d), cap, T0, gate=FakeGate(), log=lambda *_: None)
            calls = []; st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
            self.assertEqual(T.upload(db, st, d, apply=False, log=lambda *_: None), 1); self.assertEqual(calls, [])   # 既定は計画だけ
            self.assertEqual(T.upload(db, st, d, apply=True, log=lambda *_: None), 1); self.assertEqual(len(calls), 1)
            self.assertEqual(T.upload(db, st, d, apply=True, log=lambda *_: None), 0)   # 印が付いたので 2 度目は置かない
            self.assertIn(f"thumb_path='{T.thumb_name('a')}'", T.sql_updates(db, store='r2-local'))
            T.run(db, T.LocalDirStore(d), cap, T0 + datetime.timedelta(days=31), gate=FakeGate(), log=lambda *_: None)   # 撮り直し(画像が変わる)
            self.assertEqual(T.sql_updates(db, store='r2-local'), '')   # R2 にはまだ古い内容 → 出さない
            self.assertEqual(T.upload(db, st, d, apply=True, log=lambda *_: None), 1)   # 同じキーへ上書き
            self.assertEqual(T.sql_updates(db, store='r2-local').count('UPDATE sites'), 1)


class Delete(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.db = T.connect(':memory:'); self.addCleanup(self.db.close); T.import_sites(self.db, [('a', 'https://a.example/'), ('b', 'https://b.example/')])
        T.run(self.db, T.LocalDirStore(self.dir.name), fake_capturer({'https://a.example/': dict(OK), 'https://b.example/': dict(OK)}), T0, gate=FakeGate(), log=lambda *_: None)

    def test_dry_run_changes_nothing(self):
        calls = []
        n = T.delete_sites(self.db, T.R2Store(runner=lambda a: (calls.append(a), 0)[1]), ['a'], apply=False, log=lambda *_: None)
        self.assertEqual((n, calls), (0, []))
        self.assertEqual(self.db.execute("SELECT exclude FROM thumbs WHERE site_id='a'").fetchone()[0], 0)

    def test_apply_deletes_one_object_excludes_and_nulls_in_sql_without_touching_others(self):
        calls = []
        T._mark(self.db, T.thumb_name('a'), 'r2-local', 'x'); T._mark(self.db, T.thumb_name('b'), 'r2-local', 'x')
        self.assertEqual(T.delete_sites(self.db, T.R2Store(runner=lambda a: (calls.append(a), 0)[1]), ['a'], apply=True, log=lambda *_: None), 1)
        self.assertEqual(len(calls), 1); self.assertIn('mise-media/thumb/' + T.thumb_name('a'), calls[0])   # 1 本だけ(他サイトの参照確認は要らない)
        r = self.db.execute("SELECT exclude, exclude_reason, thumb_hash, lqip FROM thumbs WHERE site_id='a'").fetchone()
        self.assertEqual((r['exclude'], r['exclude_reason'], r['thumb_hash'], r['lqip']), (1, 'deleted', None, None))
        self.assertFalse(T._marked(self.db, T.thumb_name('a'), 'r2-local')); self.assertTrue(T._marked(self.db, T.thumb_name('b'), 'r2-local'))
        self.assertIn("thumb_path=NULL", T.sql_updates(self.db, store='local')); self.assertEqual(T.due_sites(self.db, T0 + datetime.timedelta(days=99), 10)[0]['site_id'], 'b')

    def test_local_delete_and_cli_dry_run(self):
        self.assertTrue((Path(self.dir.name) / T.thumb_name('a')).is_file())
        T.delete_sites(self.db, T.LocalDirStore(self.dir.name), ['a'], apply=True, log=lambda *_: None)
        self.assertFalse((Path(self.dir.name) / T.thumb_name('a')).is_file()); self.assertTrue((Path(self.dir.name) / T.thumb_name('b')).is_file())
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.main(['--db', str(Path(d) / 'none.sqlite'), 'delete', '--site', 'a'])
            self.assertFalse((Path(d) / 'none.sqlite').exists())


class Sandbox(unittest.TestCase):
    """起動コマンドの締め付け(実際の撮影・隔離の実測は scripts/thumbnail/selftest.py)。"""
    def cmd(self, sandbox=True):
        c = T.NodeCapturer(sandbox=sandbox, log=lambda *_: None); c.scope = True
        return c.command('/tmp/mise-thumb-x', 'https://a.example/', ['https://a.example/'], mock.Mock(url='http://127.0.0.1:1'))

    def test_bwrap_flags(self):
        c = self.cmd(); s = ' '.join(c)
        for flag in ('--die-with-parent', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--unshare-net', '--new-session', '--clearenv', '--ro-bind / /', '--tmpfs /home', '--tmpfs /tmp', '--tmpfs /run', '--bind /tmp/mise-thumb-x /tmp/mise-thumb-x'):
            self.assertIn(flag, s)
        self.assertIn('systemd-run --user --scope', s); self.assertIn('MemoryMax=', s); self.assertIn('CPUQuota=', s)
        for t in ('--tmpfs /var/tmp', '--tmpfs /opt', '--tmpfs /srv', '--tmpfs /sys', '--ro-bind /sys/devices/system/cpu /sys/devices/system/cpu'):
            self.assertIn(t, s)
        self.assertNotIn('--dev-bind', s)   # ホストのデバイス・ルートを書き込み可で見せない
        self.assertNotIn('--no-sandbox', s)
        self.assertIn('--proxy-unix', s); self.assertNotIn('--proxy ', s)   # 外へ出る道はフィルタへの Unix ソケットだけ

    def test_no_sandbox_mode_uses_tcp_proxy(self):
        s = ' '.join(self.cmd(False))
        self.assertNotIn('bwrap', s); self.assertIn('--proxy http://127.0.0.1:1', s)

    def test_chromium_launch_args_force_proxy_and_sandbox(self):
        js = (ROOT / 'scripts' / 'thumbnail' / 'capture.mjs').read_text(encoding='utf-8')
        for needle in ('chromiumSandbox: true', '--proxy-server=', '--proxy-bypass-list=<-loopback>', '--host-resolver-rules=MAP * ~NOTFOUND', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', '--disable-quic'):
            self.assertIn(needle, js)
        self.assertNotIn("'--no-sandbox'", js)
        self.assertNotIn('proxy: {', js)   # Playwright の proxy オプション(bypass が付く)は使わない


class Limits(unittest.TestCase):
    def test_limits_unavailable_is_refused_unless_no_limits(self):
        with mock.patch.object(T, '_systemd_scope_ok', return_value=False):
            with self.assertRaises(RuntimeError):
                T.NodeCapturer(sandbox=True, log=lambda *_: None).check_ready()
            T.NodeCapturer(sandbox=True, limits=False, log=lambda *_: None).check_ready()   # 明示すれば進む
            c = T.NodeCapturer(sandbox=True, limits=False, log=lambda *_: None); self.assertNotIn('systemd-run', ' '.join(c.command('/tmp/x', 'https://a.example/', ['https://a.example/'], mock.Mock(url='x'))))

    def test_cli_apply_exits_3_without_limits_and_no_limits_flag_runs(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(T, '_systemd_scope_ok', return_value=False):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(T.main(['--db', str(Path(d) / 't.sqlite'), 'run', '--apply']), 3)
            self.assertIn('--no-limits', err.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(T.main(['--db', str(Path(d) / 't.sqlite'), 'run', '--apply', '--no-limits']), 0)

    def test_scope_check_requires_memory_max_to_take_effect(self):
        cg = tempfile.TemporaryDirectory(); self.addCleanup(cg.cleanup)
        limit = 512 * 1024 * 1024
        ok = mock.Mock(returncode=0, stdout='0::/user.slice/run-x.scope\n')
        with mock.patch.object(T.subprocess, 'run', return_value=ok), mock.patch.object(T, 'Path', side_effect=lambda *a: Path(*a) if a != ('/sys/fs/cgroup',) else Path(cg.name)):
            (Path(cg.name) / 'user.slice' / 'run-x.scope').mkdir(parents=True)
            (Path(cg.name) / 'user.slice' / 'run-x.scope' / 'memory.max').write_text(f'{limit}\n')
            self.assertTrue(T._systemd_scope_ok(limit))
            (Path(cg.name) / 'user.slice' / 'run-x.scope' / 'memory.max').write_text('max\n')
            self.assertFalse(T._systemd_scope_ok(limit))   # 委譲されず上限が効いていない
        with mock.patch.object(T.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='')):
            self.assertFalse(T._systemd_scope_ok(limit))

    def test_socket_dir_is_short_even_when_tmpdir_is_long(self):
        with mock.patch('tempfile.gettempdir', return_value='/tmp/' + 'x' * 100):
            self.assertEqual(T.job_tmp_base(), '/tmp')
        with mock.patch('tempfile.gettempdir', return_value='/var/tmp'):
            self.assertEqual(T.job_tmp_base(), '/var/tmp')


class ExcludeAndUpload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.db = T.connect(':memory:'); self.addCleanup(self.db.close); T.import_sites(self.db, [('a', 'https://a.example/'), ('b', 'https://b.example/')])
        T.run(self.db, T.LocalDirStore(self.dir.name), fake_capturer({'https://a.example/': dict(OK), 'https://b.example/': dict(OK)}), T0, gate=FakeGate(), log=lambda *_: None)

    def test_upload_never_uploads_excluded_sites(self):
        T.set_exclude(self.db, ['a'])
        calls = []; st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
        self.assertEqual(T.upload(self.db, st, self.dir.name, apply=True, log=lambda *_: None), 1)
        self.assertEqual(len(calls), 1); self.assertIn('mise-media/thumb/' + T.thumb_name('b'), calls[0]); self.assertFalse(any(T.thumb_name('a') in ' '.join(c) for c in calls))
        self.assertEqual(T.upload(self.db, st, self.dir.name, apply=False, log=lambda *_: None), 0)

    def test_exclude_of_uploaded_site_warns_and_points_to_delete(self):
        dbp = str(Path(self.dir.name) / 'x.sqlite'); db = T.connect(dbp); T.import_sites(db, [('a', 'https://a.example/'), ('b', 'https://b.example/')])
        T._mark(db, T.thumb_name('a'), 'r2-remote', 'c'); T._mark(db, T.thumb_name('b'), 'local', 'c'); db.commit(); db.close()   # a は R2 に置いた、b はローカルだけ
        self.assertEqual(T.uploaded_sites(T.connect(dbp), ['a', 'b']), ['a'])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            T.main(['--db', dbp, 'import', '--exclude', 'a,b'])
        e = err.getvalue(); self.assertIn('R2 にアップロード済み', e); self.assertIn('a', e); self.assertIn('D1 が先、R2 が後', e); self.assertNotIn('delete --site a', e)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            T.main(['--db', dbp, 'import', '--exclude', 'a', '--delete-uploaded'])
        self.assertIn('delete --site a --r2-remote', err.getvalue())   # dry-run の delete を案内(--apply は付けない)

    def test_delete_apply_requires_d1_done_for_r2(self):
        dbp = str(Path(self.dir.name) / 'y.sqlite'); db = T.connect(dbp); T.import_sites(db, [('a', 'https://a.example/')]); db.close()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), mock.patch.object(T, 'R2Store', side_effect=AssertionError('must not touch R2')):
            self.assertEqual(T.main(['--db', dbp, 'delete', '--site', 'a', '--apply']), 2)
        self.assertIn('--d1-done', err.getvalue())

    def test_unpinned_note_is_recorded_on_success_and_failure(self):
        rec = dict(OK, proxy={'over_limit': False, 'blocked': [], 'unpinned': ['a.example']})
        r = T.attempt('https://a.example/', FakeGate(), lambda u, a: (rec, png()))
        self.assertEqual(r['notes'], 'upstream_dns_unpinned: a.example')
        db = T.connect(':memory:'); self.addCleanup(db.close); T.import_sites(db, [('a', 'https://a.example/')])
        row = db.execute('SELECT * FROM thumbs').fetchone()
        T.record(db, T.LocalDirStore(self.dir.name), row, r, T0)
        self.assertEqual(db.execute('SELECT notes FROM thumbs').fetchone()[0], 'upstream_dns_unpinned: a.example')
        fail = dict(status='error', error='timeout', notes='upstream_dns_unpinned: a.example')
        T.record(db, T.LocalDirStore(self.dir.name), db.execute('SELECT * FROM thumbs').fetchone(), fail, T0)
        le = db.execute('SELECT last_error FROM thumbs').fetchone()[0]
        self.assertEqual(le, 'timeout [upstream_dns_unpinned: a.example]'); self.assertEqual(T.error_code(le), 'timeout')
        self.assertEqual(T.retry_days(le, 1), T.RETRY_DAYS)


class Cli(unittest.TestCase):
    def test_exclude_unknown_id_warns(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = str(Path(d) / 't.sqlite'); err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                T.main(['--db', dbp, 'import', '--exclude', 'nope,zzz'])
            self.assertIn('台帳に無い id', err.getvalue()); self.assertIn('nope', err.getvalue())

    def test_dry_run_creates_nothing_and_never_launches_browser(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = Path(d) / 'sub' / 't.sqlite'
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(T, 'NodeCapturer', side_effect=AssertionError('browser')):
                self.assertEqual(T.main(['--db', str(dbp), 'run', '--limit', '5']), 0)
            self.assertIn('[dry-run] 撮影対象 0 件', out.getvalue())
            self.assertFalse(dbp.exists()); self.assertFalse(dbp.parent.exists())

    def test_import_and_dry_run_lists(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = str(Path(d) / 't.sqlite'); f = Path(d) / 's.json'
            f.write_text(json.dumps([{'id': 'a', 'url': 'https://a.example/'}, {'id': 'b', 'url': 'https://b.example/'}, {'id': 'c'}]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.main(['--db', dbp, 'import', '--sites', str(f), '--exclude', 'b'])
                T.main(['--db', dbp, 'run'])
                T.main(['--db', dbp, 'status'])
            s = out.getvalue()
            self.assertIn('imported=2', s); self.assertIn('excluded=1', s); self.assertIn('撮影対象 1 件', s); self.assertIn('excluded\t1', s)
            self.assertNotIn('b.example', s)

    def test_apply_without_pillow_would_stop(self):
        # --apply の前提(Pillow・bwrap)確認のコードパスがあること(実際の撮影はしない)
        with mock.patch.dict(sys.modules, {'PIL': None}):
            self.assertEqual(T.main(['--db', ':memory:', 'run', '--apply']), 3)

    def test_default_per_day_and_concurrency(self):
        self.assertEqual((T.DEFAULT_PER_DAY, T.DEFAULT_CONCURRENCY), (3000, 2))
        self.assertEqual((T.THUMB_W, T.THUMB_H), (320, 200))


if __name__ == '__main__':
    unittest.main()
