"""URL 発見ランナー(scripts/discover_online.py)。ネットワーク・本番 D1 には出ない(D1 は in-memory SQLite の偽クライアント)。"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import discover_online as do
import discover_wikidata as dw
import fetcher

FIXTURE = (ROOT / 'tests' / 'fixtures' / 'discover_online' / 'directory.html').read_text(encoding='utf-8')
URL_PROGRESS_DDL = re.search(r'CREATE TABLE IF NOT EXISTS url_progress \(.*?\n\);', (ROOT / 'migrations' / '0032_url_progress.sql').read_text(encoding='utf-8'), re.S).group(0)
CURSORS_DDL = (ROOT / 'migrations' / '0037_discovery_cursors.sql').read_text(encoding='utf-8')


class FakeD1:
    """media_online.Cloudflare の query/batch だけを in-memory SQLite で再現する。1 文 100 パラメータ超は D1 と同じく失敗させる。"""
    def __init__(self, drop_inserts=False, with_cursors=True):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute(URL_PROGRESS_DDL)
        self.db.execute('CREATE TABLE sites (id INTEGER PRIMARY KEY, url TEXT, progress_key TEXT)')
        if with_cursors:
            self.db.executescript(CURSORS_DDL)
        self.drop_inserts = drop_inserts
        self.statements = []
        self.max_params = 0

    def query(self, sql, params=()):
        params = list(params)
        self.max_params = max(self.max_params, len(params))
        if len(params) > 100:
            raise RuntimeError('too many SQL variables')
        self.statements.append(sql)
        if self.drop_inserts and sql.startswith('INSERT OR IGNORE INTO url_progress'):
            return []
        try:
            cur = self.db.execute(sql, params)
        except sqlite3.OperationalError as e:
            raise RuntimeError(str(e))
        rows = [dict(r) for r in cur.fetchall()]
        self.db.commit()
        return rows

    def batch(self, statements):
        return [self.query(sql, params) for sql, params in statements]

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM url_progress').fetchone()[0]


def serve(pages):
    """fetcher.http_get の代わり。robots.txt は 404(許可)、pages 以外は 404。"""
    def http_get(url, max_body=0, extra_headers=None):
        if url.endswith('/robots.txt'):
            return 404, {}, b''
        if url in pages:
            return 200, {'content-type': 'text/html; charset=utf-8'}, pages[url].encode('utf-8')
        return 404, {}, b''
    return http_get


ADAPTER = [{'name': 'fake', 'label': '架空', 'category': 'fake-cat', 'urls': ['https://example-assoc.or.jp/members/']}]


class Normalize(unittest.TestCase):
    def test_top_url_key_and_filters(self):
        self.assertEqual(do.candidate_from_url('https://www.Alpha-Corp.co.jp/company/about.html#x'), ('alpha-corp.co.jp/', 'https://www.alpha-corp.co.jp/'))   # キーは www なし、保存 URL は元のホスト
        self.assertEqual(do.candidate_from_url('http://beta.example.jp'), ('beta.example.jp/', 'http://beta.example.jp/'))
        for bad in ['ftp://a.co.jp/', 'http://192.168.0.5/', 'http://[::1]/', 'http://printer.local/', 'http://localhost/', 'http://intranet/', 'https://omega.co.jp:8443/', 'mailto:a@b.co.jp', '', None,
                    'https://www.instagram.com/someshop/']:
            self.assertIsNone(do.candidate_from_url(bad), bad)

    def test_extract_member_links_skips_noise(self):
        links = do.extract_member_links(FIXTURE, 'https://example-assoc.or.jp/members/')
        hosts = [do.urllib.parse.urlsplit(u).hostname for u in links]
        self.assertIn('www.alpha-corp.co.jp', hosts)
        self.assertIn('eta.co.jp', hosts)
        for gone in ['www.facebook.com', 'twitter.com', 'www.instagram.com', 'www.google.com', 'news.example-assoc.or.jp', 'members.example-assoc.or.jp', 'footer-only.co.jp', 'epsilon.co.jp']:
            self.assertNotIn(gone, hosts)   # SNS / 検索 / 協会自身 / <main> の外 / PDF
        self.assertFalse(any(u.startswith('mailto:') or u.endswith('.pdf') for u in links))


class Dedupe(unittest.TestCase):
    def sink(self, d1, apply=True, limit=None):
        return do.Sink(d1, apply, do.Stats(), None, limit)

    def test_dedupe_within_run_and_against_known(self):
        d1 = FakeD1()
        d1.db.execute("INSERT INTO url_progress(identity_key,url) VALUES('gamma.co.jp/','https://gamma.co.jp/')")
        d1.db.execute("INSERT INTO sites(url,progress_key) VALUES('https://old.co.jp','old.co.jp')")   # 旧形(末尾 / なし)
        s = self.sink(d1)
        for u in ['https://www.alpha-corp.co.jp/a', 'https://alpha-corp.co.jp/b', 'https://gamma.co.jp/x', 'https://old.co.jp/', 'https://new.co.jp/', 'http://192.168.1.1/']:
            s.offer('official', 'c', u)
        s.flush()
        st = s.stats.rows['official']
        self.assertEqual((st['fetched'], st['invalid'], st['duplicate_in_run'], st['already_known'], st['added'], st['verified']), (6, 1, 1, 2, 2, 2))
        keys = {r[0] for r in d1.db.execute('SELECT identity_key FROM url_progress')}
        self.assertEqual(keys, {'gamma.co.jp/', 'alpha-corp.co.jp/', 'new.co.jp/'})
        row = d1.db.execute("SELECT source,state FROM url_progress WHERE identity_key='new.co.jp/'").fetchone()
        self.assertEqual(tuple(row), ('discovered', 'discovered'))

    def test_param_limit_respected_for_many_urls(self):
        d1 = FakeD1()
        s = self.sink(d1)
        for i in range(400):
            s.offer('official', 'c', f'https://site{i}.co.jp/')
        s.flush()
        self.assertEqual(d1.count(), 400)
        self.assertLessEqual(d1.max_params, 100)
        self.assertEqual(s.stats.rows['official']['verified'], 400)
        s2 = self.sink(d1)       # 2 回目は全部既知
        for i in range(400):
            s2.offer('official', 'c', f'https://site{i}.co.jp/')
        s2.flush()
        self.assertEqual(s2.stats.rows['official']['already_known'], 400)
        self.assertEqual(d1.count(), 400)

    def test_limit(self):
        d1 = FakeD1()
        s = self.sink(d1, limit=3)
        for i in range(10):
            if s.room('official'):
                s.offer('official', 'c', f'https://s{i}.co.jp/')
        s.flush()
        self.assertEqual(d1.count(), 3)

    def test_verify_mismatch_exits_nonzero(self):
        d1 = FakeD1(drop_inserts=True)
        out = []
        with mock.patch.object(fetcher, 'http_get', serve({'https://example-assoc.or.jp/members/': FIXTURE})), mock.patch.object(do, 'OFFICIAL', ADAPTER):
            code, stats = do.run(d1, ('official',), True, None, pf=do.PoliteFetcher(sleep=lambda s: None, wait=0), out=out.append)
        self.assertEqual(code, 1)
        self.assertGreater(stats.mismatch, 0)


class DryRun(unittest.TestCase):
    def run_official(self, apply):
        d1 = FakeD1()
        out = []
        pages = {'https://example-assoc.or.jp/members/': FIXTURE}
        adapters = ADAPTER
        with mock.patch.object(fetcher, 'http_get', serve(pages)), mock.patch.object(do, 'OFFICIAL', adapters):
            code, stats = do.run(d1, ('official',), apply, None, pf=do.PoliteFetcher(sleep=lambda s: None, wait=0), out=out.append)
        return d1, code, stats, out

    def test_dry_run_writes_nothing(self):
        d1, code, stats, out = self.run_official(False)
        self.assertEqual(code, 0)
        self.assertEqual(d1.count(), 0)
        self.assertFalse([q for q in d1.statements if re.match(r'\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)', q, re.I)])
        self.assertEqual(stats.categories[('official', 'fake-cat')]['would_add'], 4)   # alpha(www 重複を除く), beta, gamma, eta
        self.assertTrue(any('DRY-RUN' in line for line in out))
        self.assertTrue(any('unverified:' in line for line in out))

    def test_apply_inserts_and_verifies(self):
        d1, code, stats, out = self.run_official(True)
        self.assertEqual(code, 0)
        self.assertEqual(d1.count(), 4)
        self.assertEqual(stats.rows['official']['verified'], 4)
        self.assertEqual(d1.db.execute("SELECT COUNT(*) FROM discovery_cursors WHERE source='lease'").fetchone()[0], 0)   # リース解放済み


class LeaseTest(unittest.TestCase):
    def test_second_owner_blocked_until_expiry_then_release(self):
        d1 = FakeD1()
        now = [1000.0]
        a = do.Lease(d1, owner='A', ttl=100, clock=lambda: now[0])
        b = do.Lease(d1, owner='B', ttl=100, clock=lambda: now[0])
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.renew()
        now[0] = 1200.0               # A の期限切れ
        self.assertTrue(b.acquire())
        with self.assertRaises(RuntimeError):
            a.renew()                 # A は奪われたことに気づく
        b.release()
        self.assertTrue(a.acquire())

    def test_run_refuses_when_leased(self):
        d1 = FakeD1()
        other = do.Lease(d1, owner='other')
        self.assertTrue(other.acquire())
        code, _ = do.run(d1, ('official',), True, None, out=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertEqual(d1.count(), 0)

    def test_lease_released_on_error(self):
        d1 = FakeD1()
        with mock.patch.object(do, 'run_official', side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                do.run(d1, ('official',), True, None, out=lambda *_: None)
        self.assertEqual(d1.db.execute("SELECT COUNT(*) FROM discovery_cursors WHERE source='lease'").fetchone()[0], 0)


ROW = {'leaf_id': 'shrines', 'qid': 'Q845945', 'rel': 'P31', 'country_prop': 'P17|P495', 'subclass': '1', 'label_en': 'Shinto shrine', 'note': '', 'max_rows': '100'}


def wd_result(*items):
    return json.dumps({'results': {'bindings': [{'item': {'value': f'http://www.wikidata.org/entity/{q}'}, 'url': {'value': u}} for q, u in items]}}).encode()


class WikidataTest(unittest.TestCase):
    def wdqs(self, pages):
        calls = []
        def http_get(url):
            calls.append(url)
            body = pages.pop(0)
            if isinstance(body, int):
                return body, {'Retry-After': '30'}, b''
            return 200, {}, body
        w = do.WdqsClient(sleep=lambda s: None, http_get=http_get)
        w.calls = calls
        return w

    def test_cursor_in_d1_and_resume(self):
        d1 = FakeD1()
        stats = do.Stats()
        sink = do.Sink(d1, True, stats)
        cursors = do.Cursors(d1, True)
        w = self.wdqs([wd_result(('Q1', 'https://a.example.jp/x'), ('Q2', 'http://192.168.0.1/'), ('Q3', 'https://www.instagram.com/shop')), wd_result(('Q4', 'https://b.co.jp/'))])
        with mock.patch.object(dw, 'validate_map', return_value=[]):
            do.run_wikidata(sink, cursors, w, [ROW], 1000, page_size=3)
        self.assertEqual({r[0] for r in d1.db.execute('SELECT identity_key FROM url_progress')}, {'a.example.jp/', 'b.co.jp/'})
        self.assertEqual(stats.rows['wikidata']['invalid'], 2)
        cur = dw.cursor_name(ROW)
        self.assertEqual(d1.db.execute("SELECT cursor_value FROM discovery_cursors WHERE source='wikidata' AND cursor_key=?", (cur,)).fetchone()[0], 'done')
        # 取り終えた行は再実行でネットワークに出ない
        w2 = self.wdqs([])
        with mock.patch.object(dw, 'validate_map', return_value=[]):
            do.run_wikidata(do.Sink(d1, True, do.Stats()), do.Cursors(d1, True), w2, [ROW], 1000, page_size=3)
        self.assertEqual(w2.calls, [])

    def test_abort_keeps_cursor_and_dry_run_does_not_write_cursor(self):
        d1 = FakeD1()
        sink = do.Sink(d1, True, do.Stats())
        w = self.wdqs([429])
        with mock.patch.object(dw, 'validate_map', return_value=[]):
            aborted = do.run_wikidata(sink, do.Cursors(d1, True), w, [ROW], 1000)
        self.assertIn('429', aborted)
        self.assertEqual(d1.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0], 0)
        w = self.wdqs([wd_result(('Q1', 'https://a.example.jp/'))])
        sink = do.Sink(d1, False, do.Stats())
        with mock.patch.object(dw, 'validate_map', return_value=[]):
            do.run_wikidata(sink, do.Cursors(d1, False), w, [ROW], 1000)
        self.assertEqual(d1.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0], 0)
        self.assertEqual(d1.count(), 0)

    def test_shipped_map_is_valid_for_runner(self):
        self.assertEqual(dw.validate_map(dw.load_map()), [])


class NoLocalState(unittest.TestCase):
    def test_creates_no_files(self):
        repo_var = ROOT / 'var'
        before_var = sorted(p.name for p in repo_var.rglob('*')) if repo_var.exists() else None
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                d1 = FakeD1()
                w = WikidataTest().wdqs([wd_result(('Q1', 'https://a.example.jp/'))])
                with mock.patch.object(fetcher, 'http_get', serve({'https://example-assoc.or.jp/members/': FIXTURE})), \
                        mock.patch.object(do, 'OFFICIAL', ADAPTER), mock.patch.object(dw, 'validate_map', return_value=[]):
                    code, _ = do.run(d1, ('wikidata', 'official', 'links'), True, None, wdqs=w, rows=[ROW],
                                     pf=do.PoliteFetcher(sleep=lambda s: None, wait=0), out=lambda *_: None)
                self.assertEqual(code, 0)
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(old)
        after_var = sorted(p.name for p in repo_var.rglob('*')) if repo_var.exists() else None
        self.assertEqual(before_var, after_var)

    def test_source_has_no_local_state_writers(self):
        text = (ROOT / 'scripts' / 'discover_online.py').read_text(encoding='utf-8')
        imports = ' '.join(re.findall(r'^(?:import|from)\s+(\w+)', text, re.M))
        for mod in ['sqlite3', 'csv', 'tempfile', 'shutil', 'pickle', 'shelve']:
            self.assertNotIn(mod, imports.split(), mod)
        for token in ['open(', 'write_text', 'write_bytes', 'mkdir', '.jsonl']:
            self.assertNotIn(token, text, token)


if __name__ == '__main__':
    unittest.main()
