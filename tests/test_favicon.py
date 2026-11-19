"""ファビコン取得(#125)。ネットワークは fetcher.http_get の差し替えで全てモックする。実行: uv run --with pillow python3 -m unittest tests.test_favicon"""
import datetime
import io
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'favicon'))
import favicon
from favicon import fetcher
from PIL import Image

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 20, tzinfo=UTC)


def img(fmt='PNG', size=(64, 64), color=(200, 30, 30, 255), **kw):
    b = io.BytesIO()
    Image.new('RGBA', size, color).save(b, fmt, **kw)
    return b.getvalue()


def html(*links):
    return ('<html><head>' + ''.join(links) + '</head><body>x</body></html>').encode()


class Normalize(unittest.TestCase):
    def test_resize_to_32_webp(self):
        out = favicon.normalize_icon(img(size=(180, 180)), 'image/png')
        im = Image.open(io.BytesIO(out))
        self.assertEqual((im.format, im.size), ('WEBP', (32, 32)))

    def test_non_square_is_padded(self):
        im = Image.open(io.BytesIO(favicon.normalize_icon(img(size=(64, 32)))))
        self.assertEqual(im.size, (32, 32))
        self.assertEqual(im.convert('RGBA').getpixel((16, 0))[3], 0)   # 上の余白は透明

    def test_formats(self):
        for fmt in ('PNG', 'GIF', 'WEBP', 'JPEG', 'BMP'):
            data = img(fmt, color=(10, 20, 30, 255)) if fmt != 'JPEG' else Image.new('RGB', (48, 48), (1, 2, 3))
            if fmt == 'JPEG':
                b = io.BytesIO(); data.save(b, 'JPEG'); data = b.getvalue()
            self.assertEqual(Image.open(io.BytesIO(favicon.normalize_icon(data))).size, (32, 32), fmt)

    def test_ico_uses_largest_frame(self):
        b = io.BytesIO()
        Image.new('RGBA', (48, 48), (0, 200, 0, 255)).save(b, 'ICO', sizes=[(16, 16), (32, 32), (48, 48)])
        self.assertEqual(Image.open(io.BytesIO(favicon.normalize_icon(b.getvalue()))).size, (32, 32))

    def test_svg_rejected(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32"/></svg>'
        for ct in ('image/svg+xml', 'application/octet-stream'):
            with self.assertRaises(favicon.IconError) as c:
                favicon.normalize_icon(svg, ct)
            self.assertEqual(c.exception.code, 'svg_rejected')

    def test_html_soft_404_rejected(self):
        with self.assertRaises(favicon.IconError) as c:
            favicon.normalize_icon(b'<!DOCTYPE html><html>not found</html>', 'text/html')
        self.assertEqual(c.exception.code, 'not_image')

    def test_broken_and_limits(self):
        cases = [(b'\x89PNG\r\n\x1a\nGARBAGE', 'bad_image'), (img(size=(8, 8)), 'too_small'), (img(size=(2000, 2000)), 'too_large_dim'),
                 (b'\x00' * (favicon.MAX_ICON_BYTES + 1), 'too_large'), (img(color=(0, 0, 0, 0)), 'blank'), (b'', 'not_image')]
        for data, code in cases:
            with self.assertRaises(favicon.IconError, msg=code) as c:
                favicon.normalize_icon(data, 'image/png')
            self.assertEqual(c.exception.code, code)

    @staticmethod
    def ico_with_png(declared, png):
        return struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', declared, declared, 0, 0, 1, 32, len(png), 22) + png

    def test_ico_declared_small_but_embedded_huge_rejected(self):
        big = img(size=(2500, 2500), color=(1, 2, 3, 255))
        with self.assertRaises(favicon.IconError) as c:
            favicon.normalize_icon(self.ico_with_png(16, big), 'image/x-icon')
        self.assertIn(c.exception.code, ('too_large_dim', 'bad_image'))

    def test_embedded_actual_aspect_used(self):
        out = favicon.normalize_icon(self.ico_with_png(32, img(size=(64, 32))), 'image/x-icon')   # 宣言 32x32、実寸 64x32
        im = Image.open(io.BytesIO(out)).convert('RGBA')
        self.assertEqual(im.size, (32, 32))
        self.assertEqual(im.getpixel((16, 0))[3], 0)      # 上下は余白
        self.assertEqual(im.getpixel((0, 16))[3], 255)    # 左右は絵が端まで

    def test_disallowed_format_rejected_at_open(self):
        b = io.BytesIO(); Image.new('RGB', (32, 32), (1, 2, 3)).save(b, 'TIFF')
        with self.assertRaises(favicon.IconError) as c:
            favicon.normalize_icon(b.getvalue(), 'image/tiff')
        self.assertEqual(c.exception.code, 'bad_image')

    def test_max_image_pixels_restored(self):
        before = Image.MAX_IMAGE_PIXELS
        favicon.normalize_icon(img())
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)

    def test_deterministic_name(self):
        a = favicon.icon_name(favicon.normalize_icon(img(size=(64, 64))))
        self.assertEqual(a, favicon.icon_name(favicon.normalize_icon(img(size=(64, 64)))))
        self.assertTrue(a.endswith('.webp'))


class Candidates(unittest.TestCase):
    def test_priority_and_relative_urls(self):
        h = html('<link rel="icon" href="/s16.png" sizes="16x16">', '<link rel="shortcut icon" href="plain.ico">',
                 '<link rel="apple-touch-icon" href="/apple.png" sizes="180x180">', '<link rel="icon" href="/big.png" sizes="192x192">',
                 '<link rel="icon" href="/s32.png" sizes="32x32">', '<link rel="mask-icon" href="/mask.svg">',
                 '<link rel="icon" href="/v.svg" type="image/svg+xml">', '<link rel="icon" href="data:image/png;base64,AAAA">')
        got = favicon.icon_candidates(h.decode(), 'https://a.example/dir/page.html')
        self.assertEqual(got, ['https://a.example/s32.png', 'https://a.example/big.png', 'https://a.example/apple.png',
                               'https://a.example/dir/plain.ico', 'https://a.example/s16.png', 'https://a.example/favicon.ico'])

    def test_base_href_and_fallback_only(self):
        got = favicon.icon_candidates('<base href="https://cdn.example/assets/"><link rel=icon href=i.png>', 'https://a.example/')
        self.assertEqual(got[0], 'https://cdn.example/assets/i.png')
        self.assertEqual(favicon.icon_candidates('<html></html>', 'https://a.example/x'), ['https://a.example/favicon.ico'])


class Net(unittest.TestCase):
    def setUp(self):
        self.res = {}; self.calls = []
        self.orig = fetcher.http_get
        fetcher.http_get = self.fake
        self.addCleanup(setattr, fetcher, 'http_get', self.orig)
        self.sleeps = []
        self.clock_t = [0.0]
        self.session = favicon.Session(sleep=lambda s: (self.sleeps.append(s), self.clock_t.__setitem__(0, self.clock_t[0] + s)), clock=lambda: self.clock_t[0])

    def fake(self, url, max_body=0, extra_headers=None):
        self.calls.append((url, dict(extra_headers or {})))
        if url in self.res:
            r = self.res[url]
            if isinstance(r, Exception):
                raise r
            return r
        if url.endswith('/robots.txt'):
            return (404, {}, b'')
        if 'example' in url:
            return (404, {}, b'')
        return self.orig(url, max_body, extra_headers)   # 実物: 公開でないホストは通信前に ssrf_blocked

    def put(self, url, status=200, ctype='image/png', body=b'', **h):
        self.res[url] = (status, {'content-type': ctype, **h}, body)

    def page(self, *links, url='https://a.example/'):
        self.put(url, ctype='text/html', body=html(*links))

    def fetch(self, prev=None):
        return favicon.fetch_favicon(self.session, 'https://a.example/', prev)

    def test_link_icon_then_ok(self):
        self.page('<link rel=icon href="/i.png" sizes="64x64">')
        self.put('https://a.example/i.png', body=img(), etag='"v1"', **{'last-modified': 'Mon, 01 Jan 2026 00:00:00 GMT'})
        r = self.fetch()
        self.assertEqual((r['status'], r['icon_url'], r['etag']), ('ok', 'https://a.example/i.png', '"v1"'))

    def test_falls_back_to_favicon_ico_and_to_next_candidate(self):
        self.page('<link rel=icon href="/bad.png" sizes="32x32">')
        self.put('https://a.example/bad.png', body=b'\x89PNGxxxx')
        self.put('https://a.example/favicon.ico', ctype='image/x-icon', body=img('GIF'))
        self.assertEqual(self.fetch()['icon_url'], 'https://a.example/favicon.ico')

    def test_no_icon_gives_none(self):
        self.page()
        r = self.fetch()
        self.assertEqual(r['status'], 'none')

    def test_svg_only_site_gets_nothing(self):
        self.page('<link rel=icon href="/v.svg" type="image/svg+xml">')
        self.put('https://a.example/favicon.ico', ctype='image/svg+xml', body=b'<svg xmlns="x"/>')
        self.assertEqual(self.fetch()['status'], 'none')

    def test_robots_disallow_page(self):
        self.put('https://a.example/robots.txt', ctype='text/plain', body=b'User-agent: *\nDisallow: /\n')
        r = self.fetch()
        self.assertEqual((r['status'], r['error']), ('error', 'robots_disallowed'))
        self.assertFalse(any(u == 'https://a.example/' for u, _ in self.calls))

    def test_robots_disallow_icon_path(self):
        self.put('https://a.example/robots.txt', ctype='text/plain', body=b'User-agent: *\nDisallow: /i.png\n')
        self.page('<link rel=icon href="/i.png">')
        self.put('https://a.example/i.png', body=img())
        self.assertEqual(self.fetch()['status'], 'none')
        self.assertFalse(any(u.endswith('/i.png') for u, _ in self.calls))

    def test_ssrf_private_link_skipped_then_favicon_ico_tried(self):
        self.page('<link rel=icon href="http://127.0.0.1/i.png">', '<link rel=icon href="http://10.0.0.5/j.png">')
        self.put('https://a.example/favicon.ico', body=img())
        r = self.fetch()
        self.assertEqual((r['status'], r['icon_url']), ('ok', 'https://a.example/favicon.ico'))   # 内部アドレスの候補は試さず次へ

    def test_all_candidates_blocked_is_error(self):
        self.page('<link rel=icon href="http://127.0.0.1/i.png">')
        self.res['https://a.example/favicon.ico'] = (302, {'location': 'http://10.0.0.5/x'}, b'')
        r = self.fetch()
        self.assertEqual((r['status'], r['error']), ('error', 'ssrf_blocked'))

    def test_ssrf_redirect_to_private(self):
        self.res['https://a.example/'] = (302, {'location': 'http://127.0.0.1/'}, b'')
        r = self.fetch()
        self.assertEqual((r['status'], r['error']), ('error', 'redirect_offsite'))   # 同一サイト外は追わない
        self.page('<link rel=icon href="/i.png">')
        self.res['https://a.example/i.png'] = (302, {'location': 'http://169.254.169.254/latest'}, b'')
        self.assertEqual(self.fetch()['error'], 'ssrf_blocked')   # 画像の転送先は他ホスト可だが、内部アドレスは拒否

    def test_icon_on_other_host_allowed_via_cdn(self):
        self.page('<link rel=icon href="https://cdn.example.net/i.png">')
        self.put('https://cdn.example.net/i.png', body=img())
        self.assertEqual(self.fetch()['status'], 'ok')

    def test_too_large_icon(self):
        self.page('<link rel=icon href="/i.png">')
        self.put('https://a.example/i.png', body=b'\x00' * (favicon.MAX_ICON_BYTES + 1))
        self.assertEqual(self.fetch()['status'], 'none')

    def test_conditional_get_304(self):
        self.put('https://a.example/i.png', status=304, ctype='')
        r = self.fetch({'icon_url': 'https://a.example/i.png', 'etag': '"v1"', 'last_modified': 'Mon, 01 Jan 2026 00:00:00 GMT'})
        self.assertEqual(r['status'], 'unchanged')
        sent = [h for u, h in self.calls if u.endswith('/i.png')][0]
        self.assertEqual(sent, {'If-None-Match': '"v1"', 'If-Modified-Since': 'Mon, 01 Jan 2026 00:00:00 GMT'})
        self.assertFalse(any(u == 'https://a.example/' for u, _ in self.calls))   # ページは取らない

    def test_stale_icon_url_rediscovers(self):
        self.put('https://a.example/old.png', status=404)
        self.page('<link rel=icon href="/new.png">')
        self.put('https://a.example/new.png', body=img())
        r = self.fetch({'icon_url': 'https://a.example/old.png'})
        self.assertEqual(r['icon_url'], 'https://a.example/new.png')

    def test_host_wait_between_requests(self):
        self.page('<link rel=icon href="/i.png">')
        self.put('https://a.example/i.png', body=img())
        self.fetch()
        self.assertTrue(self.sleeps and all(s >= 1.9 for s in self.sleeps))

    def test_top_page_5xx_is_transient_and_skips_favicon_ico(self):
        self.res['https://a.example/'] = (503, {}, b'')
        self.put('https://a.example/favicon.ico', body=img())
        self.assertEqual(self.fetch(), {'status': 'error', 'error': 'http_503'})
        self.assertFalse(any(u.endswith('/favicon.ico') for u, _ in self.calls))

    def test_conditional_headers_only_on_first_hop_and_crlf_stripped(self):
        self.res['https://a.example/i.png'] = (302, {'location': 'https://a.example/j.png'}, b'')
        self.put('https://a.example/j.png', body=img())
        r = self.fetch({'icon_url': 'https://a.example/i.png', 'etag': '"v\r\nX-Evil: 1"'})
        self.assertEqual(r['status'], 'ok')
        sent = dict(self.calls)
        self.assertEqual(sent['https://a.example/i.png'], {'If-None-Match': '"vX-Evil: 1"'})
        self.assertEqual(sent['https://a.example/j.png'], {})

    def test_connect_error_is_transient(self):
        self.res['https://a.example/'] = fetcher.FetchError('connect', 'timed out')
        self.assertEqual(self.fetch(), {'status': 'error', 'error': 'connect'})


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = favicon.LocalDirStore(Path(self.tmp.name) / 'icons')
        self.db = favicon.connect(':memory:')
        favicon.import_sites(self.db, [('s1', 'https://a.example/'), ('s2', 'https://b.example/')])

    def row(self, sid):
        return self.db.execute('SELECT * FROM favicons WHERE site_id=?', (sid,)).fetchone()

    def ok(self, color=(200, 0, 0, 255)):
        return {'status': 'ok', 'icon_url': 'https://x.example/i.png', 'etag': 'e', 'webp': favicon.normalize_icon(img(color=color))}

    def test_unchanged_hash_does_not_write_or_touch_changed_at(self):
        res = self.ok()
        self.assertEqual(favicon.record(self.db, self.store, self.row('s1'), res, T0), 'changed')
        name = self.row('s1')['icon_hash']
        written = (Path(self.tmp.name) / 'icons' / name).stat().st_mtime_ns
        t1 = T0 + datetime.timedelta(days=31)
        self.assertEqual(favicon.record(self.db, self.store, self.row('s1'), self.ok(), t1), 'same')
        self.assertEqual((Path(self.tmp.name) / 'icons' / name).stat().st_mtime_ns, written)
        self.assertEqual(self.row('s1')['changed_at'], '2026-11-20T00:00:00Z')
        self.assertEqual(self.row('s1')['next_check_at'], '2027-01-20T00:00:00Z')   # 確認した日から 30 日後

    def test_same_image_shared_between_sites(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        favicon.record(self.db, self.store, self.row('s2'), self.ok(), T0)
        self.assertEqual(self.row('s1')['icon_hash'], self.row('s2')['icon_hash'])
        self.assertEqual(len(list((Path(self.tmp.name) / 'icons').iterdir())), 1)

    def test_changed_icon_overwrites_hash(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        old = self.row('s1')['icon_hash']
        self.assertEqual(favicon.record(self.db, self.store, self.row('s1'), self.ok((0, 0, 200, 255)), T0), 'changed')
        self.assertNotEqual(old, self.row('s1')['icon_hash'])

    def test_failure_keeps_previous_icon_and_backoff(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        h = self.row('s1')['icon_hash']
        favicon.record(self.db, self.store, self.row('s1'), {'status': 'error', 'error': 'connect'}, T0)
        r = self.row('s1')
        self.assertEqual((r['icon_hash'], r['consecutive_failures'], r['next_check_at']), (h, 1, '2026-11-23T00:00:00Z'))
        favicon.record(self.db, self.store, self.row('s1'), {'status': 'error', 'error': 'robots_disallowed'}, T0)
        self.assertEqual(self.row('s1')['next_check_at'], '2026-12-20T00:00:00Z')

    def test_due_selection_and_dry_run_sql(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        due = [r['site_id'] for r in favicon.due_sites(self.db, T0 + datetime.timedelta(days=1), 10)]
        self.assertEqual(due, ['s2'])   # s1 は 30 日後まで対象外
        due = [r['site_id'] for r in favicon.due_sites(self.db, T0 + datetime.timedelta(days=31), 10)]
        self.assertEqual(set(due), {'s1', 's2'})

    def test_sql_only_for_sites_with_icon(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        sql = favicon.sql_updates(self.db)
        self.assertIn("WHERE id='s1' AND favicon_path IS NOT", sql)
        self.assertNotIn('s2', sql)
        self.assertEqual(favicon.sql_updates(self.db, '2026-12-01T00:00:00Z'), '')

    def test_import_from_recrawl_only_listed_ok(self):
        p = Path(self.tmp.name) / 'rc.sqlite'
        c = sqlite3.connect(p)
        c.execute('CREATE TABLE sites (site_id, url, state, last_ok_at)')
        c.executemany('INSERT INTO sites VALUES (?,?,?,?)', [('r1', 'https://r1.example/', 'listed', 'x'), ('r2', 'https://r2.example/', 'needs_review', 'x'), ('r3', 'https://r3.example/', 'listed', None)])
        c.commit(); c.close()
        self.assertEqual(favicon.import_from_recrawl(self.db, p), 1)
        self.assertIsNotNone(self.row('r1')); self.assertIsNone(self.row('r2')); self.assertIsNone(self.row('r3'))

    def test_same_hash_but_missing_in_store_is_replaced(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        name = self.row('s1')['icon_hash']
        (Path(self.tmp.name) / 'icons' / name).unlink()
        self.assertEqual(favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0), 'same')
        self.assertTrue(self.store.exists(name))

    def test_304_but_missing_in_store_refetches_unconditionally(self):
        favicon.record(self.db, self.store, self.row('s1'), self.ok(), T0)
        name = self.row('s1')['icon_hash']
        (Path(self.tmp.name) / 'icons' / name).unlink()
        self.db.execute("UPDATE favicons SET next_check_at='2099-01-01T00:00:00Z' WHERE site_id='s2'")
        seen = []
        class S:
            def get(s, url, max_body, headers=None, same_site_only=False):
                seen.append(dict(headers or {}))
                if headers:
                    return 304, {}, b'', url
                return 200, {'content-type': 'image/png'}, img(color=(200, 0, 0, 255)), url
        favicon.run(self.db, self.store, T0 + datetime.timedelta(days=31), 10, session=S(), log=lambda m: None)
        self.assertEqual(seen[0], {'If-None-Match': 'e'}); self.assertEqual(seen[1], {})
        self.assertTrue(self.store.exists(name))

    def test_store_failure_is_per_site(self):
        class Bad(favicon.LocalDirStore):
            def put(self, name, data):
                raise OSError('disk full')
        class S:
            def get(s, url, max_body, headers=None, same_site_only=False):
                if url.endswith('/'):
                    return 200, {'content-type': 'text/html'}, html('<link rel=icon href="/i.png">'), url
                return 200, {'content-type': 'image/png'}, img(), url
        counts = favicon.run(self.db, Bad(self.tmp.name), T0, 10, session=S(), log=lambda m: None)
        self.assertEqual(counts, {'error': 2})
        self.assertEqual(self.row('s1')['status'], 'store_failed')
        self.assertIsNone(self.row('s1')['icon_hash'])

    def test_import_error_is_not_swallowed(self):
        class S:
            def get(s, *a, **k):
                raise ModuleNotFoundError('PIL')
        with self.assertRaises(ImportError):
            favicon.run(self.db, self.store, T0, 10, session=S(), log=lambda m: None)

    def test_readonly_commands_create_nothing(self):
        d = Path(self.tmp.name) / 'new'
        db = d / 'f.sqlite'
        for argv in (['run'], ['sql'], ['status']):
            self.assertEqual(favicon.main(['--db', str(db), *argv]), 0)
        self.assertFalse(d.exists())

    def test_run_end_to_end_mocked(self):
        class S:
            def get(s, url, max_body, headers=None, same_site_only=False):
                if url == 'https://a.example/':
                    return 200, {'content-type': 'text/html'}, html('<link rel=icon href="/i.png">'), url
                if url.endswith('/i.png'):
                    return 200, {'content-type': 'image/png'}, img(), url
                raise favicon.IconError('http_404')
        logs = []
        counts = favicon.run(self.db, self.store, T0, 10, session=S(), log=logs.append)
        self.assertEqual(counts, {'changed': 1, 'error': 1})
        self.assertEqual(self.row('s1')['status'], 'ok')
        self.assertEqual(self.row('s2')['status'], 'http_404')


class RealLaunch(unittest.TestCase):
    """夜間ジョブと同じ起動方法(uv run)で、Pillow の有無による挙動を実際に確かめる。"""
    SCRIPT = str(ROOT / 'scripts' / 'favicon' / 'favicon.py')

    def uv(self, *args, with_pillow):
        cmd = ['uv', 'run'] + (['--with', 'pillow'] if with_pillow else []) + ['python3', *args]
        env = {k: v for k, v in os.environ.items() if k not in ('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME') and not k.startswith('UV_')}   # 外側の uv run の環境を持ち込まない
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT), env=env)

    def test_pillow_importable_with_the_nightly_launch(self):
        r = self.uv('-c', 'import PIL.Image', with_pillow=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_apply_without_pillow_exits_nonzero_not_silently(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.uv(self.SCRIPT, '--db', str(Path(t) / 'f.sqlite'), 'run', '--apply', '--limit', '1', with_pillow=False)
            self.assertEqual(r.returncode, 3)
            self.assertIn('Pillow', r.stderr)

    def test_apply_with_pillow_runs(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.uv(self.SCRIPT, '--db', str(Path(t) / 'f.sqlite'), 'run', '--apply', '--limit', '1', '--store-dir', str(Path(t) / 's'), with_pillow=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == '__main__':
    unittest.main()
