"""供給元 links / cc(scripts/discover_links_cc.py)。ネットワーク・本番 D1 には出ない(偽 D1 は test_discover_online と共通)。"""
import gzip
import io
import os
import re
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
sys.path.insert(0, str(ROOT / 'tests'))
import discover_links_cc as dlc
import discover_online as do
from test_discover_online import FakeD1, serve

TOP = (ROOT / 'tests' / 'fixtures' / 'discover_links_cc' / 'site_top.html').read_text(encoding='utf-8')
CC_KEY = f'{dlc.CC_GRAPH}/part37/every10'


def pf():
    return do.PoliteFetcher(sleep=lambda s: None, wait=0)


def add_sites(d1, urls):
    for i, u in enumerate(urls, 1):
        d1.db.execute('INSERT INTO sites(id,url,progress_key) VALUES(?,?,?)', (f'fr-site{i}-{i:08x}', u, u))
    d1.db.commit()


def urls(d1):
    return sorted(r[0] for r in d1.db.execute('SELECT url FROM url_progress'))


def cursor(d1, source, key):
    r = d1.db.execute('SELECT cursor_value FROM discovery_cursors WHERE source=? AND cursor_key=?', (source, key)).fetchone()
    return r[0] if r else None


class Filters(unittest.TestCase):
    def test_personal_and_tiny_blog_pass(self):
        got = dlc.extract_outbound(TOP, 'https://example-site.jp/')
        self.assertIn('https://yamada-taro.jp/', got)          # 個人名ドメイン
        self.assertIn('https://tarou.hatenablog.com/', got)    # 小さなブログ(共有サービス配下)
        self.assertIn('https://hanako.hatenablog.com/', got)
        self.assertIn('https://uni.ac.jp/~sato/', got)         # 共有ホストのユーザーページはパスが 1 サイト
        self.assertEqual(got.count('https://yamada-taro.jp/'), 1)

    def test_only_entrances_and_safety_rejects_removed(self):
        got = dlc.extract_outbound(TOP, 'https://example-site.jp/')
        for bad in ['twitter.com', 't.co', 'apple.com', 'cloudfront.net', '192.168', '127.0.0.1', 'secret.example.jp', '8080', 'www.hatenablog.com']:
            self.assertFalse(any(bad in u for u in got), bad)

    def test_cc_host_filter_same(self):
        self.assertIsNone(dlc.entrance_reason('yamada-taro.jp'))
        self.assertIsNone(dlc.entrance_reason('tarou.hatenablog.com'))
        self.assertEqual(dlc.entrance_reason('www.facebook.com'), 'entrance')

    def test_no_popularity_in_source(self):
        text = (ROOT / 'scripts' / 'discover_links_cc.py').read_text(encoding='utf-8')
        code = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#'))
        for token in ['priority(', 'rank', 'popular', 'pagerank', 'harmonic', 'indegree']:
            self.assertNotIn(token, code, token)
        self.assertIn('ORDER BY id', code)   # links の順序は sites.id(機械的)


class SiteIdentity(unittest.TestCase):
    def test_same_site_is_same_host_only(self):
        self.assertTrue(dlc.same_site('www.a.jp', 'a.jp'))
        self.assertFalse(dlc.same_site('a.sakura.ne.jp', 'b.sakura.ne.jp'))
        self.assertFalse(dlc.same_site('example-site.jp', 'sub.example-site.jp'))
        got = dlc.extract_outbound('<a href="https://b.sakura.ne.jp/">x</a><a href="https://www.a.sakura.ne.jp/">y</a>', 'https://a.sakura.ne.jp/')
        self.assertEqual(got, ['https://b.sakura.ne.jp/'])

    def test_medium_apex_is_entrance_but_user_blog_passes(self):
        self.assertEqual(dlc.entrance_reason('medium.com'), 'platform_apex')
        self.assertEqual(dlc.entrance_reason('www.medium.com'), 'platform_apex')
        self.assertIsNone(dlc.entrance_reason('alice.medium.com'))


class LinksTest(unittest.TestCase):
    def setUp(self):
        self.d1 = FakeD1()
        add_sites(self.d1, ['https://example-site.jp/', 'https://second.example.jp/', 'https://third.example.jp/'])
        self.pages = {'https://example-site.jp/': TOP,
                      'https://second.example.jp/': '<a href="https://hobby-page.example.com/">x</a>',
                      'https://third.example.jp/': '<a href="https://zzz.example.org/">x</a>'}

    def run_links(self, apply, limit=None, **extra):
        with mock.patch.object(do.fetcher, 'http_get', serve(self.pages)):
            return do.run(self.d1, ('links',), apply, limit, pf=pf(), out=lambda *_: None, extra=extra)

    def test_dry_run_writes_nothing(self):
        code, stats = self.run_links(False)
        self.assertEqual(code, 0)
        self.assertEqual(self.d1.count(), 0)
        self.assertGreaterEqual(stats.rows['links']['would_add'], 5)
        self.assertEqual(self.d1.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0], 0)

    def test_apply_slices_and_cursor_in_d1(self):
        code, _ = self.run_links(True, max_sites=1)
        self.assertEqual(code, 0)
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site1-00000001')
        self.assertIn('https://yamada-taro.jp/', urls(self.d1))
        self.assertNotIn('https://hobby-page.example.com/', urls(self.d1))
        self.run_links(True, max_sites=1)
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site2-00000002')
        self.assertIn('https://hobby-page.example.com/', urls(self.d1))
        _, stats = self.run_links(True, max_sites=5)
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site3-00000003')
        self.assertIn('https://zzz.example.org/', urls(self.d1))
        self.assertEqual(stats.mismatch, 0)

    def test_second_run_resumes_from_text_cursor_and_done_note(self):
        self.run_links(True, max_sites=1)
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site1-00000001')
        order = []
        real = do.PoliteFetcher.get

        def spy(self_, url):
            order.append(url)
            return real(self_, url)
        with mock.patch.object(do.PoliteFetcher, 'get', spy):
            _, stats = self.run_links(True, max_sites=1)   # 2 回目: 文字列カーソルから 2 件目だけ(飛ばさず・繰り返さず)
        self.assertEqual(order, ['https://second.example.jp/'])
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site2-00000002')
        with mock.patch.object(do.PoliteFetcher, 'get', spy):
            self.run_links(True, max_sites=5)
        self.assertEqual(order[1:], ['https://second.example.jp/', 'https://third.example.jp/'][1:])
        _, stats = self.run_links(True, max_sites=5)   # 全部読み終えた後は 0 サイト(呼び出し側の終了判定)
        self.assertTrue(any('links: 処理 0 サイト(' in n for n in stats.notes))

    def test_tilde_path_reaches_url_progress_and_cc_arg_validated(self):
        self.run_links(True)
        self.assertIn('https://uni.ac.jp/~sato/', urls(self.d1))
        self.assertNotIn('https://uni.ac.jp/', urls(self.d1))
        self.assertIn('https://sub.example-site.jp/', urls(self.d1))
        with self.assertRaises(SystemExit):
            do.main(['--source', 'cc', '--cc-sample-every', '0'])

    def test_order_is_by_id_not_popularity(self):
        order = []
        real = do.PoliteFetcher.get

        def spy(self_, url):
            order.append(url)
            return real(self_, url)
        with mock.patch.object(do.PoliteFetcher, 'get', spy):
            self.run_links(False)
        self.assertEqual(order, ['https://example-site.jp/', 'https://second.example.jp/', 'https://third.example.jp/'])

    def test_limit_does_not_lose_links_and_retries_site(self):
        code, _ = self.run_links(True, limit=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(urls(self.d1)), 2)
        self.assertIsNone(cursor(self.d1, 'links', 'sites_id'))   # 上限で捨てた候補があるサイトはカーソルを進めない
        self.run_links(True)
        self.assertEqual(len(urls(self.d1)), 7)

    def test_unfetchable_site_still_advances(self):
        self.pages.pop('https://second.example.jp/')
        self.run_links(True)
        self.assertEqual(cursor(self.d1, 'links', 'sites_id'), 'fr-site3-00000003')


def cc_opener(hosts, calls=None):
    paths = [f'projects/x/part-{i:05d}.txt.gz' for i in range(40)]

    def open_lines(url):
        if calls is not None:
            calls.append(url)
        if url.endswith('.paths.gz'):
            return iter(paths)
        return iter(f'{i}\t{h}' for i, h in enumerate(hosts))
    return open_lines


class CcTest(unittest.TestCase):
    def hosts(self, n=3000):
        return ['com.example'] + [f'jp.site{i}' for i in range(n)] + ['jp.twitter', 'jp.yamada-taro', 'jp.facebook.www']

    def run_cc(self, d1, hosts, apply=True, every=10, limit=None, calls=None):
        return do.run(d1, ('cc',), apply, limit, pf=pf(), out=lambda *_: None, extra={'cc_open': cc_opener(hosts, calls), 'cc_sample_every': every})

    def test_samples_by_hash_only_jp_and_filters_entrances(self):
        d1, hosts = FakeD1(), self.hosts()
        code, _ = self.run_cc(d1, hosts)
        self.assertEqual(code, 0)
        want = {f'https://{dlc.from_reversed(h)}/' for h in hosts if h.startswith('jp.') and zlib.crc32(h.encode()) % 10 == 0
                and not dlc.entrance_reason(dlc.from_reversed(h))}
        self.assertEqual(set(urls(d1)), want)
        self.assertEqual(cursor(d1, 'cc', CC_KEY), 'done')
        self.assertFalse(any('twitter' in u or 'facebook' in u or 'example.com' in u for u in urls(d1)))

    def test_personal_name_and_tiny_blog_hosts_pass(self):
        d1 = FakeD1()
        self.run_cc(d1, ['jp.yamada-taro', 'com.hatenablog.tarou'], every=1)
        self.assertEqual(urls(d1), ['https://yamada-taro.jp/'])   # com.* は .jp ではないので読まない(従来どおり日本のサイトに絞る)
        d2 = FakeD1()
        self.run_cc(d2, ['jp.hatenablog.tarou', 'jp.ne.sakura.hanako'], every=1)
        self.assertEqual(urls(d2), ['https://hanako.sakura.ne.jp/', 'https://tarou.hatenablog.jp/'])

    def test_limit_resume_covers_everything_exactly_once(self):
        d1, full, hosts = FakeD1(), FakeD1(), self.hosts()
        self.run_cc(full, hosts)
        for _ in range(100):
            self.run_cc(d1, hosts, limit=25)
            if cursor(d1, 'cc', CC_KEY) == 'done':
                break
        self.assertEqual(urls(d1), urls(full))
        calls = []
        self.run_cc(d1, hosts, calls=calls)
        self.assertEqual(calls, [])   # 読み終えたらダウンロードしない

    def test_dry_run_writes_nothing(self):
        d1 = FakeD1()
        _, stats = self.run_cc(d1, self.hosts(), apply=False)
        self.assertEqual(d1.count(), 0)
        self.assertGreater(stats.rows['cc']['would_add'], 0)
        self.assertEqual(d1.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0], 0)


class NoLocalState(unittest.TestCase):
    def test_no_files_created(self):
        repo_var = ROOT / 'var'
        before = sorted(p.name for p in repo_var.rglob('*')) if repo_var.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                d1 = FakeD1()
                add_sites(d1, ['https://example-site.jp/'])
                hosts = [f'jp.h{i}' for i in range(200)]
                with mock.patch.object(do.fetcher, 'http_get', serve({'https://example-site.jp/': TOP})):
                    code, _ = do.run(d1, ('links', 'cc'), True, None, pf=pf(), out=lambda *_: None,
                                     extra={'cc_open': cc_opener(hosts), 'cc_sample_every': 5})
                self.assertEqual(code, 0)
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(old)
        self.assertEqual(before, sorted(p.name for p in repo_var.rglob('*')) if repo_var.exists() else None)

    def test_source_has_no_local_state_writers(self):
        text = (ROOT / 'scripts' / 'discover_links_cc.py').read_text(encoding='utf-8')
        imports = ' '.join(re.findall(r'^(?:import|from)\s+(\w+)', text, re.M)).split()
        for mod in ['sqlite3', 'csv', 'tempfile', 'shutil', 'pickle', 'shelve', 'gzip']:
            self.assertNotIn(mod, imports, mod)
        for token in [' open(', 'write_text', 'write_bytes', 'mkdir', '.jsonl', "'-o'"]:
            self.assertNotIn(token, text, token)

    def test_curl_lines_streams_gzip(self):
        data = gzip.compress(b'a\tjp.x\nb\tjp.y\nc\tjp.z')

        class P:
            stdout = io.BytesIO(data)

            def wait(self):
                return 0

            def poll(self):
                return 0
        with mock.patch.object(dlc.subprocess, 'Popen', return_value=P()):
            self.assertEqual(list(dlc.curl_lines('https://x/')), ['a\tjp.x', 'b\tjp.y', 'c\tjp.z'])


if __name__ == '__main__':
    unittest.main()
