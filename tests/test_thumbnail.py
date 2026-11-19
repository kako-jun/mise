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
    """名前解決の模擬(ネットワークなし)。リテラル IP と localhost は実際の判定どおり、それ以外の名前は公開アドレスに解決されたことにする。"""
    import ipaddress
    h = host.strip('[]')
    try:
        ips = [h]
    except ValueError:
        ips = []
    if h == 'localhost':
        ips = ['127.0.0.1']
    else:
        try:
            ipaddress.ip_address(h)
        except ValueError:
            ips = ['93.184.216.34']
    for ip in ips:
        if not ipaddress.ip_address(ip).is_global:
            raise fetcher.FetchError('ssrf_blocked', f'{host} -> non-public address')
    return ips


def setUpModule():
    global _patch
    _patch = mock.patch.object(fetcher, 'check_public_host', _resolve); _patch.start()


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
    """robots / 待ちを持たない門(ネットワークなし)。disallow に入れたホストは robots_disallowed。"""
    def __init__(self, disallow=(), unavailable=()):
        import threading
        self.disallow = set(disallow); self.unavailable = set(unavailable); self.checked = []; self.waited = []
        self.locks = {}; self.guard = threading.Lock()

    def host_lock(self, host):
        with self.guard:
            return self.locks.setdefault(host, __import__('threading').Lock())

    def robots(self, url):
        host = T.urllib.parse.urlsplit(url).hostname; self.checked.append(url)
        return 'robots_disallowed' if host in self.disallow else ('robots_unavailable' if host in self.unavailable else None)

    def wait(self, host, delay=0):
        self.waited.append(host)

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
        self.assertRegex(T.thumb_name(webp), r'^[0-9a-f]{32}\.webp$')

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

    def test_same_image_same_name(self):
        self.assertEqual(T.thumb_name(T.make_thumb(png())[0]), T.thumb_name(T.make_thumb(png())[0]))


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


class Attempt(unittest.TestCase):
    def test_ok(self):
        gate = FakeGate(); cap = fake_capturer({'https://a.example/': dict(OK)})
        r = T.attempt('https://a.example/', gate, cap)
        self.assertEqual(r['status'], 'ok'); self.assertTrue(r['webp']); self.assertTrue(r['lqip'])
        self.assertEqual(cap.calls, [('https://a.example/', {'https://a.example'})])
        self.assertEqual(gate.waited, ['a.example'])

    def test_ssrf_precheck_never_calls_browser(self):
        gate = FakeGate(); cap = fake_capturer({})
        for url in ('http://127.0.0.1/', 'http://10.1.2.3/', 'http://localhost/', 'file:///etc/passwd', 'ftp://a.example/', 'https://a.example:8443/', 'http://[::1]/'):
            r = T.attempt(url, gate, cap)
            self.assertEqual(r['status'], 'error', url)
            self.assertIn(r['error'], ('ssrf_blocked', 'bad_url', 'bad_port', 'dns'), url)
        self.assertEqual(cap.calls, []); self.assertEqual(gate.checked, [])

    def test_robots_disallow_and_unavailable(self):
        cap = fake_capturer({'https://a.example/': dict(OK)})
        self.assertEqual(T.attempt('https://a.example/', FakeGate(disallow={'a.example'}), cap)['error'], 'robots_disallowed')
        self.assertEqual(T.attempt('https://a.example/', FakeGate(unavailable={'a.example'}), cap)['error'], 'robots_unavailable')
        self.assertEqual(cap.calls, [])

    def test_origin_change_checks_robots_of_new_origin_then_retries_once(self):
        gate = FakeGate(); cap = fake_capturer({'https://a.example/': [
            {'ok': False, 'reason': 'origin-changed:https://www.a.example'}, dict(OK)]})
        r = T.attempt('https://a.example/', gate, cap)
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(gate.checked, ['https://a.example/', 'https://www.a.example/'])
        self.assertEqual(cap.calls[1][1], {'https://a.example', 'https://www.a.example'})

    def test_origin_change_to_disallowed_origin_stops(self):
        gate = FakeGate(disallow={'www.a.example'}); cap = fake_capturer({'https://a.example/': [{'ok': False, 'reason': 'origin-changed:https://www.a.example'}]})
        self.assertEqual(T.attempt('https://a.example/', gate, cap)['error'], 'robots_disallowed')
        self.assertEqual(len(cap.calls), 1)

    def test_origin_change_to_other_site_or_ssrf_is_not_followed(self):
        for new in ('https://evil.example', 'http://10.0.0.1'):
            gate = FakeGate(); cap = fake_capturer({'https://a.example/': [{'ok': False, 'reason': 'origin-changed:' + new}]})
            r = T.attempt('https://a.example/', gate, cap)
            self.assertEqual(r['status'], 'error'); self.assertEqual(len(cap.calls), 1, new)
            self.assertEqual(gate.checked, ['https://a.example/'])

    def test_origin_hops_are_limited(self):
        gate = FakeGate(); cap = fake_capturer({'https://a.example/': [{'ok': False, 'reason': 'origin-changed:https://www.a.example'}] * 5})
        T.attempt('https://a.example/', gate, cap)
        self.assertLessEqual(len(cap.calls), T.MAX_ORIGIN_HOPS + 1)

    def test_blank_page_is_failure(self):
        cap = lambda url, allow: ({'ok': True}, png(noisy=False))
        self.assertEqual(T.attempt('https://a.example/', FakeGate(), cap)['error'], 'blank')

    def test_browser_reason_passthrough_and_obscured(self):
        cap = fake_capturer({'https://a.example/': {'ok': False, 'reason': 'meta-robots:noindex', 'http_status': 200}})
        r = T.attempt('https://a.example/', FakeGate(), cap)
        self.assertEqual((r['status'], r['error']), ('error', 'meta-robots:noindex'))
        cap = fake_capturer({'https://a.example/': dict(OK, obscured_ratio=0.8)})
        self.assertTrue(T.attempt('https://a.example/', FakeGate(), cap)['obscured'])


class RobotsReal(unittest.TestCase):
    """Gate は fetcher.RobotsCache を再利用する(http_get をモック)。"""
    def gate(self, body, status=200):
        g = T.Gate(sleep=lambda s: None)
        patcher = mock.patch.object(fetcher, 'http_get', lambda url, max_body=0, extra=None: (status, {}, body))
        patcher.start(); self.addCleanup(patcher.stop)
        return g

    def test_disallow_all(self):
        g = self.gate(b'User-agent: *\nDisallow: /\n')
        self.assertEqual(g.robots('https://a.example/'), 'robots_disallowed')

    def test_own_token_disallow(self):
        g = self.gate(b'User-agent: MiseResearchBot\nDisallow: /private\n')
        self.assertEqual(g.robots('https://a.example/private/x'), 'robots_disallowed')
        self.assertIsNone(g.robots('https://a.example/'))

    def test_404_means_allowed_and_5xx_unavailable_403_disallowed(self):
        self.assertIsNone(self.gate(b'', 404).robots('https://a.example/'))
        self.assertEqual(self.gate(b'', 503).robots('https://b.example/'), 'robots_unavailable')
        self.assertEqual(self.gate(b'', 403).robots('https://c.example/'), 'robots_disallowed')

    def test_same_host_gap_at_least_two_seconds(self):
        slept = []; now = [100.0]
        g = T.Gate(sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
        g.wait('a.example'); now[0] += 0.5; g.wait('a.example')
        self.assertAlmostEqual(slept[0], 1.5)
        self.assertGreaterEqual(T.HOST_WAIT, 2.0)


class StoreAndSql(unittest.TestCase):
    def setUp(self):
        self.db = T.connect(':memory:'); T.import_sites(self.db, [('a', 'https://a.example/'), ('b', 'https://b.example/')]); self.addCleanup(self.db.close)
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)

    def go(self, store, cap=None):
        cap = cap or fake_capturer({'https://a.example/': dict(OK), 'https://b.example/': dict(OK)})
        return T.run(self.db, store, cap, T0, limit=10, gate=FakeGate(), log=lambda *_: None)

    def test_local_store_marks_and_dedups(self):
        self.go(T.LocalDirStore(self.dir.name))
        files = list(Path(self.dir.name).glob('*.webp'))
        self.assertEqual(len(files), 1)   # 同じ画像(同じ模様)は 1 ファイルに重複排除
        names = {r['thumb_hash'] for r in self.db.execute('SELECT thumb_hash FROM thumbs')}
        self.assertEqual(names, {files[0].name})
        self.assertEqual({r['store'] for r in self.db.execute('SELECT store FROM thumb_stored')}, {'local'})
        self.assertEqual(self.db.execute("SELECT state FROM thumbs WHERE site_id='a'").fetchone()[0], 'ok')

    def test_sql_default_only_r2_remote(self):
        self.go(T.LocalDirStore(self.dir.name))
        self.assertEqual(T.sql_updates(self.db, store='r2-remote'), '')   # ローカルにしか無い画像は出さない
        sql = T.sql_updates(self.db, store='local')
        self.assertEqual(sql.count('UPDATE sites'), 2)
        h = self.db.execute("SELECT thumb_hash FROM thumbs WHERE site_id='a'").fetchone()[0]
        self.assertIn(f"thumb_path='{h}'", sql)
        self.assertRegex(sql, r"thumb_lqip='data:image/webp;base64,[A-Za-z0-9+/=]+'")
        self.assertRegex(sql, r"thumb_captured_at='2026-11-20T03:00:00Z'")
        self.assertIn(f"AND thumb_path IS NOT '{h}';", sql)
        T._mark(self.db, h, 'r2-remote')
        self.assertEqual(T.sql_updates(self.db, store='r2-remote').count('UPDATE sites'), 2)
        self.assertEqual(T.sql_updates(self.db, since='2099-01-01T00:00:00Z', store='r2-remote'), '')

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

    def test_sql_skips_excluded_and_quotes(self):
        self.go(T.LocalDirStore(self.dir.name))
        T.set_exclude(self.db, ['a'])
        sql = T.sql_updates(self.db, store='local')
        self.assertNotIn("id='a'", sql); self.assertIn("id='b'", sql)
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
        self.assertIn(f"thumb_path='{h}'", T.sql_updates(self.db, store='local'))   # 失敗しても前回の画像は消えない

    def test_recapture_unchanged_does_not_touch_changed_at(self):
        self.go(T.LocalDirStore(self.dir.name))
        before = self.db.execute("SELECT changed_at FROM thumbs WHERE site_id='a'").fetchone()[0]
        later = T0 + datetime.timedelta(days=31)
        T.run(self.db, T.LocalDirStore(self.dir.name), fake_capturer({'https://a.example/': dict(OK), 'https://b.example/': dict(OK)}), later, gate=FakeGate(), log=lambda *_: None)
        r = self.db.execute("SELECT changed_at, captured_at FROM thumbs WHERE site_id='a'").fetchone()
        self.assertEqual(r['changed_at'], before); self.assertEqual(r['captured_at'], T.now_iso(later))

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

    def test_concurrency_runs_all_and_same_host_is_serial(self):
        import threading, time
        T.import_sites(self.db, [(f's{i}', f'https://h{i % 2}.example/p{i}') for i in range(6)])
        active = {}; overlap = []; lock = threading.Lock()

        def cap(url, allow):
            host = T.urllib.parse.urlsplit(url).hostname
            with lock:
                if active.get(host):
                    overlap.append(host)
                active[host] = True
            time.sleep(0.02)
            with lock:
                active[host] = False
            return dict(OK), png()
        c = T.run(self.db, T.LocalDirStore(self.dir.name), cap, T0, limit=20, concurrency=3, gate=FakeGate(), log=lambda *_: None)
        self.assertEqual(sum(c.values()), 8); self.assertEqual(overlap, [])


class R2(unittest.TestCase):
    def test_key_prefix_and_local_default(self):
        calls = []
        st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
        self.assertTrue(st.put('a' * 32 + '.webp', b'x'))
        a = calls[0]
        self.assertIn('mise-media/thumb/' + 'a' * 32 + '.webp', a); self.assertIn('--local', a); self.assertNotIn('--remote', a)
        self.assertEqual(st.label, 'r2-local')
        calls.clear(); st2 = T.R2Store(remote=True, runner=lambda a: (calls.append(a), 0)[1]); st2.put('b' * 32 + '.webp', b'x')
        self.assertIn('--remote', calls[0]); self.assertEqual(st2.label, 'r2-remote')

    def test_upload_from_local_dir_only_unmarked_and_dry_run_default(self):
        with tempfile.TemporaryDirectory() as d:
            db = T.connect(':memory:'); T.import_sites(db, [('a', 'https://a.example/')])
            T.run(db, T.LocalDirStore(d), fake_capturer({'https://a.example/': dict(OK)}), T0, gate=FakeGate(), log=lambda *_: None)
            calls = []; st = T.R2Store(runner=lambda a: (calls.append(a), 0)[1])
            self.assertEqual(T.upload(db, st, d, apply=False, log=lambda *_: None), 1); self.assertEqual(calls, [])   # 既定は計画だけ
            self.assertEqual(T.upload(db, st, d, apply=True, log=lambda *_: None), 1); self.assertEqual(len(calls), 1)
            self.assertEqual(T.upload(db, st, d, apply=True, log=lambda *_: None), 0)   # 印が付いたので 2 度目は置かない
            h = db.execute('SELECT thumb_hash FROM thumbs').fetchone()[0]
            self.assertIn(f"thumb_path='{h}'", T.sql_updates(db, store='r2-local'))


class Cli(unittest.TestCase):
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
