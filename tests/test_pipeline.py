import pathlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / 'pipeline'))
import datetime
import sqlite3
import features, fetcher, frontier, hostnames, run


class Hosts(unittest.TestCase):
    def test_reversed(self):
        self.assertEqual(hostnames.from_reversed('jp.example.www'), 'www.example.jp')
        self.assertIsNone(hostnames.from_reversed('jp.exa_mple'))

    def test_prefilter(self):
        for host, want in [('www.city.kanazawa.ishikawa.jp', 'government'), ('example.go.jp', 'government'), ('foo.lg.jp', 'government'),
                           ('toyota.co.jp', None), ('u.ac.jp', None), ('k.ed.jp', None), ('blog.example.jp', None),
                           ('blog.ameblo.jp', 'shared_service_user'), ('ameblo.jp', None), ('foo-clinic.jp', None), ('example.co.jp', None), ('sample-porn.jp', None),
                           ('example.com', 'not_jp'), ('hyuki.jp', None), ('www.hyuki.ne.jp', None), ('1.2.3.4', 'invalid_host')]:
            self.assertEqual(hostnames.prefilter(host), want, host)


class Links(unittest.TestCase):
    def test_non_jp_allowed_for_links(self):
        self.assertIsNone(hostnames.prefilter('hyuki.com', jp_only=False))
        self.assertEqual(hostnames.prefilter('foo.github.io', jp_only=False), 'shared_service_user')
        self.assertIsNone(hostnames.prefilter('github.io', jp_only=False))
        self.assertIsNone(hostnames.prefilter('foo.co.uk', jp_only=False))
        self.assertEqual(hostnames.registrable('a.b.com.au'), 'b.com.au')


class Ssrf(unittest.TestCase):
    def test_private_literal_rejected(self):
        for h in ['127.0.0.1', 'localhost', '10.0.0.1', '169.254.169.254']:
            with self.assertRaises(fetcher.FetchError):
                fetcher.check_public_host(h)


class Judge(unittest.TestCase):
    PAGE = '<html lang="ja"><head><title>わたしの趣味のページ</title><meta name="description" content="個人の日記です"></head><body>' + 'こんにちは、ここは管理人の雑記帳です。' * 20 + '</body></html>'

    def judged(self, html, headers=None):
        f = features.extract('example.jp', html, headers or {})
        return features.judge(f)

    def test_candidate(self):
        self.assertEqual(self.judged(self.PAGE), ('candidate', None))

    def test_noindex(self):
        self.assertEqual(self.judged(self.PAGE, {'x-robots-tag': 'noindex'}), ('rejected', 'noindex'))

    def test_parking(self):
        self.assertEqual(self.judged(self.PAGE.replace('こんにちは', 'このドメインは売り物です')), ('rejected', 'parking_or_default_page'))

    def test_thin(self):
        self.assertEqual(self.judged('<html><title>あいうえお</title>こんにちは</html>'), ('rejected', 'thin_content'))

    def test_ad_blog_passes_but_ad_dominated_page_rejected(self):
        ads = lambda n: '<script src="https://pagead2.googlesyndication.com/x.js"></script>' * n
        blog = self.PAGE.replace('こんにちは、ここは管理人の雑記帳です。', 'こんにちは、ここは管理人の雑記帳です。' * 12)   # 本文が長い個人ブログ
        self.assertEqual(self.judged(blog.replace('</body>', ads(7) + '</body>')), ('candidate', None))
        self.assertEqual(self.judged(self.PAGE.replace('</body>', ads(4) + '</body>')), ('rejected', 'ad_heavy'))   # 本文より広告が多い
        self.assertEqual(self.judged(blog.replace('</body>', ads(15) + '</body>')), ('rejected', 'ad_heavy'))        # テンプレ級

    def test_ads(self):
        ads = '<script src="https://pagead2.googlesyndication.com/x.js"></script>' * 5
        self.assertEqual(self.judged(self.PAGE.replace('</body>', ads + '</body>')), ('rejected', 'ad_heavy'))


class Kind(unittest.TestCase):
    def test_site_kind(self):
        self.assertEqual(features.site_kind('x.or.jp', '', ''), '団体')
        self.assertEqual(features.site_kind('x.jp', 'NPO法人あいう', ''), '団体')
        self.assertEqual(features.site_kind('x.jp', '株式会社あいう', ''), '企業')
        self.assertEqual(features.site_kind('x.or.jp', '株式会社あいう', ''), '企業')
        self.assertEqual(features.site_kind('x.jp', '私のページ', ''), '不明')

    def test_small_business_not_rejected(self):
        f = features.extract('x.jp', '<html lang=ja><title>株式会社あいう 工務店</title>' + 'ご相談はお気軽に。' * 40, {})
        self.assertEqual(features.judge(f), ('candidate', None))


class Adult(unittest.TestCase):
    def test_adult_is_attribute_not_rejection(self):
        html = '<html lang=ja><head><title>同人サークルのページ</title><meta name="rating" content="adult"></head><body>' + 'ここは個人の創作サークルです。' * 30 + '年齢確認 18歳以上' + '</body></html>'
        f = features.extract('circle.jp', html, {})
        self.assertEqual(features.judge(f), ('candidate', None))
        self.assertEqual(features.adult_attr({**f, 'host': 'circle.jp'}), 'yes')
        legal = features.extract('x.jp', Judge.PAGE.replace('個人の日記', '出会い系サイトの誹謗中傷のご相談'), {})
        self.assertEqual(features.adult_attr({**legal, 'host': 'law.jp'}), 'unknown')   # 弱い語の単独一致では yes にしない
        two = features.extract('x.jp', Judge.PAGE.replace('個人の日記', 'アダルト 風俗'), {})
        self.assertEqual(features.adult_attr({**two, 'host': 'x.jp'}), 'yes')
        g = features.extract('x.jp', Judge.PAGE, {})
        self.assertEqual(features.adult_attr({**g, 'host': 'x.jp'}), 'unknown')


class Robots(unittest.TestCase):
    def test_token(self):
        import urllib.robotparser
        p = urllib.robotparser.RobotFileParser(); p.parse(['User-agent: MiseResearchBot', 'Disallow: /'])
        self.assertFalse(p.can_fetch(fetcher.ROBOT_TOKEN, 'https://x.jp/'))


RAW = b'HTTP/1.1 200 Connection established\r\n\r\nHTTP/1.1 100 Continue\r\n\r\nHTTP/2 200\r\nContent-Type: text/html; charset=Shift_JIS\r\nX-Robots-Tag: noindex\r\n\r\nBODY'


class Parse(unittest.TestCase):
    def test_skips_connect_and_1xx(self):
        status, headers, body = fetcher._parse_response(RAW, 1000)
        self.assertEqual((status, body), (200, b'BODY'))
        self.assertEqual(headers['x-robots-tag'], 'noindex')

    def test_body_cap(self):
        self.assertEqual(fetcher._parse_response(b'HTTP/1.1 200 OK\r\n\r\n' + b'x' * 50, 10)[2], b'x' * 10)

    def test_protocol_error(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher._parse_response(b'garbage', 10)

    def test_decode_body(self):
        text = 'こんにちは、日本語のページです'
        self.assertEqual(fetcher.decode_body(text.encode('cp932'), {'content-type': 'text/html; charset=Shift_JIS'}), text)
        self.assertEqual(fetcher.decode_body(text.encode('euc-jp'), {}), text)
        self.assertEqual(fetcher.decode_body(text.encode('utf-8'), {}), text)
        meta = b'<meta charset="euc-jp">' + text.encode('euc-jp')
        self.assertIn(text, fetcher.decode_body(meta, {}))


class Net(unittest.TestCase):
    """http_get / check_public_host を差し替えてネットワークなしで取得ロジックを検査する。"""
    def setUp(self):
        self.responses = {}
        self.orig = (fetcher.http_get, fetcher.time.sleep)
        fetcher.http_get = self.fake; fetcher.time.sleep = lambda s: None

    def tearDown(self):
        fetcher.http_get, fetcher.time.sleep = self.orig

    def fake(self, url, max_body=0):
        r = self.responses.get(url)
        if r is None:
            raise fetcher.FetchError('connect', url)
        return r

    def robots(self, text, status=200):
        return (status, {}, text.encode())

    def test_robots_states(self):
        for status, body, want in [(404, '', ('allowed_no_robots', True)), (403, '', ('disallowed', False)), (503, '', ('unavailable', False)),
                                   (200, 'User-agent: *\nDisallow: /private', ('allowed', True))]:
            self.responses = {'https://a.jp/robots.txt': (status, {}, body.encode())}
            st, ok, _ = fetcher.RobotsCache(lambda h: None).check('https://a.jp', '/')
            self.assertEqual((st, ok), want)

    def test_robots_disallow_and_crawl_delay(self):
        self.responses = {'https://a.jp/robots.txt': self.robots('User-agent: MiseResearchBot\nDisallow: /x\nCrawl-delay: 99')}
        c = fetcher.RobotsCache(lambda h: None)
        st, ok, delay = c.check('https://a.jp', '/x')
        self.assertFalse(ok); self.assertEqual(delay, fetcher.MAX_CRAWL_DELAY)

    def test_robots_cross_site_redirect_unavailable(self):
        self.responses = {'https://a.jp/robots.txt': (301, {'location': 'https://evil.example/robots.txt'}, b'')}
        self.assertEqual(fetcher.RobotsCache(lambda h: None).check('https://a.jp', '/')[0], 'unavailable')

    def test_follow_same_site_redirect_and_offsite(self):
        self.responses = {'https://a.jp/robots.txt': self.robots('', 404), 'https://www.a.jp/robots.txt': self.robots('', 404),
                          'https://a.jp/': (301, {'location': 'https://www.a.jp/'}, b''), 'https://www.a.jp/': (200, {}, b'ok')}
        r = fetcher.fetch_site('a.jp')
        self.assertEqual((r['error'], r['status'], r['final_url']), (None, 200, 'https://www.a.jp/'))
        self.responses['https://a.jp/'] = (302, {'location': 'https://b.example/'}, b'')
        self.assertEqual(fetcher.fetch_site('a.jp')['error'], 'redirect_offsite')

    def test_follow_redirect_blocked_by_robots_on_hop(self):
        self.responses = {'https://a.jp/robots.txt': self.robots('', 404), 'https://www.a.jp/robots.txt': self.robots('User-agent: *\nDisallow: /', 200),
                          'https://a.jp/': (301, {'location': 'https://www.a.jp/'}, b'')}
        self.assertEqual(fetcher.fetch_site('a.jp')['error'], 'robots_disallowed')

    def test_redirect_loop(self):
        self.responses = {'https://a.jp/robots.txt': self.robots('', 404), 'https://a.jp/': (301, {'location': 'https://a.jp/'}, b'')}
        self.assertEqual(fetcher.fetch_site('a.jp')['error'], 'too_many_redirects')


class Ledger(unittest.TestCase):
    def setUp(self):
        self.db = frontier.connect(':memory:') if False else None
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.db = frontier.connect(pathlib.Path(self.dir.name) / 'f.sqlite')

    def tearDown(self):
        self.db.close(); self.dir.cleanup()

    def test_add_is_idempotent_and_www_folded(self):
        self.assertTrue(frontier.add(self.db, hostnames.host_key('www.a.jp'), 'www.a.jp', 'https://www.a.jp/', 's'))
        self.assertFalse(frontier.add(self.db, hostnames.host_key('a.jp'), 'a.jp', 'https://a.jp/', 's'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 1)

    def test_triage_defers_shared_service_users(self):
        for h in ('a.jugem.jp', 'jugem.jp', 'w.example.jp', 'a.sharedhost.jp', 'b.sharedhost.jp', 'c.sharedhost.jp', 'solo.jp'):
            frontier.add(self.db, hostnames.host_key(h), h, f'https://{h}/', 's')
        run.triage(self.db)
        st = dict(self.db.execute('SELECT key, state FROM frontier').fetchall())
        self.assertEqual(st['a.jugem.jp'], 'deferred')            # 登録済みの共有サービス配下
        self.assertEqual(st['jugem.jp'], 'queued')                # サービス本体は載せてよい
        self.assertEqual(st['w.example.jp'], 'queued')            # 機能サブドメインは拒否しない
        self.assertEqual(st['a.sharedhost.jp'], 'deferred')       # 同一ドメイン配下が多い=共有サービスとみなす
        self.assertEqual(st['solo.jp'], 'queued')
        self.assertEqual(self.db.execute("SELECT reject_reason FROM frontier WHERE key='a.jugem.jp'").fetchone()[0], 'shared_service_user')

    def test_old_ledger_gets_deferred_state(self):
        path = pathlib.Path(self.dir.name) / 'old.sqlite'
        old = sqlite3.connect(path)
        old.executescript(frontier_old_schema())
        old.execute("INSERT INTO frontier (key,host,url,source,discovered_at,state) VALUES ('a.jp','a.jp','https://a.jp/','s','2026-01-01','queued')")
        old.commit(); old.close()
        db = frontier.connect(path)
        db.execute("UPDATE frontier SET state='deferred' WHERE key='a.jp'")   # 制約に通る
        self.assertEqual(db.execute('SELECT state FROM frontier').fetchone()[0], 'deferred')
        db.close()

    def test_resume_only_pending(self):
        for h in ('a.jp', 'b.jp'):
            frontier.add(self.db, h, h, f'https://{h}/', 's', state='queued')
        run.apply_result(self.db, {'key': 'a.jp', 'robots_status': 'allowed', 'status': 200, 'final_url': 'https://a.jp/', 'error': None, 'feat': None})
        states = dict(self.db.execute('SELECT key, state FROM frontier').fetchall())
        self.assertEqual(states['b.jp'], 'queued')
        self.assertNotEqual(states['a.jp'], 'queued')

    def apply(self, key, error, status=None, feat=None):
        run.apply_result(self.db, {'key': key, 'robots_status': None, 'status': status, 'final_url': None, 'error': error, 'feat': feat})
        return self.db.execute('SELECT state, reject_reason, next_fetch_at FROM frontier WHERE key=?', (key,)).fetchone()

    def test_apply_result_transitions(self):
        frontier.add(self.db, 'a.jp', 'a.jp', 'https://a.jp/', 's', state='queued')
        self.assertEqual(tuple(self.apply('a.jp', 'robots_disallowed'))[:2], ('blocked_by_robots', 'robots_disallowed'))
        self.assertEqual(tuple(self.apply('a.jp', 'redirect_offsite'))[:2], ('rejected', 'redirect_offsite'))
        self.assertEqual(tuple(self.apply('a.jp', None, 404))[:2], ('rejected', 'http_404'))
        r = self.apply('a.jp', 'dns')
        self.assertEqual((r[0], r[1]), ('failed', 'dns')); self.assertIsNotNone(r[2])
        self.assertEqual(self.apply('a.jp', None, 503)[0], 'failed')

    def test_failed_retry_and_backoff(self):
        frontier.add(self.db, 'a.jp', 'a.jp', 'https://a.jp/', 's', state='queued')
        for _ in range(run.MAX_ATTEMPTS):
            self.apply('a.jp', 'dns')
        # 待ち時間は 1, 2, 4 日。まだ明けていないので対象外
        self.assertEqual(run_fetch_targets(self.db), [])
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat(timespec='seconds')
        self.db.execute('UPDATE frontier SET next_fetch_at=?, fetch_attempts=1', (past,))
        self.assertEqual(run_fetch_targets(self.db), ['a.jp'])
        self.db.execute('UPDATE frontier SET fetch_attempts=?', (run.MAX_ATTEMPTS,))
        self.assertEqual(run_fetch_targets(self.db), [])   # 上限に達したら再取得しない


def frontier_old_schema():
    import re
    sql = (Path(frontier.__file__).parent / 'schema.sql').read_text(encoding='utf-8')
    return sql.replace(",'deferred'", '').replace(',\n  adult ', ',\n  adult ')


def run_fetch_targets(db):
    """fetch() が実際に引くホストを、取得処理を差し替えて取り出す。"""
    seen = []
    orig = run.process
    run.process = lambda row: (seen.append(row['key']) or {'key': row['key'], 'robots_status': None, 'status': None, 'final_url': None, 'error': 'dns', 'feat': None})
    try:
        run.fetch(db, 100, 1)
    finally:
        run.process = orig
    return seen


class Parking(unittest.TestCase):
    BODY = 'こんにちは、ここは管理人の雑記帳です。' * 30

    def test_weak_marker_in_long_body_is_ok(self):
        f = features.extract('x.jp', f'<html lang=ja><title>ぼくのページ</title>{self.BODY} Under Construction のコーナーがあります</html>', {})
        self.assertEqual(features.judge(f), ('candidate', None))

    def test_weak_marker_in_title_or_short_body_rejected(self):
        f = features.extract('x.jp', '<html lang=ja><title>Under Construction</title>' + self.BODY + '</html>', {})
        self.assertEqual(features.judge(f), ('rejected', 'parking_or_default_page'))
        f = features.extract('x.jp', '<html lang=ja><title>ぼくのページ</title>coming soon この文章は短い。' + 'あ' * 30 + '</html>', {})
        self.assertEqual(features.judge(f), ('rejected', 'thin_content'))   # 本文だけの弱い語は parking にしない

    def test_strong_marker_always_rejected(self):
        f = features.extract('x.jp', f'<html lang=ja><title>x</title>{self.BODY} このドメインは売り物です</html>', {})
        self.assertEqual(features.judge(f), ('rejected', 'parking_or_default_page'))

    def test_title_whitespace_folded(self):
        f = features.extract('x.jp', '<title>\n  あいう\n   えお </title>', {})
        self.assertEqual(f['title'], 'あいう えお')


if __name__ == '__main__':
    unittest.main()
