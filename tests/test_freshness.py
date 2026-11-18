"""更新の兆候の日付抽出（#129）。ネットワーク不要。"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / 'pipeline'))
import freshness as fr

TODAY = datetime.date(2026, 11, 18)


class FooterDate(unittest.TestCase):
    def f(self, text):
        return fr.footer_date(text, TODAY)

    def test_copyright_year_is_period_end(self):
        self.assertEqual(self.f('いらっしゃいませ ' * 20 + '© 2019 Taro'), '2019-12-31')

    def test_copyright_range_takes_latest(self):
        self.assertEqual(self.f('Copyright (c) 2008-2021 Hanako'), '2021-12-31')
        self.assertEqual(self.f('&copy; 2010 - 2012'), '2012-12-31')

    def test_updated_japanese(self):
        self.assertEqual(self.f('最終更新 2021年3月'), '2021-03-31')
        self.assertEqual(self.f('最終更新日:2021年3月5日'), '2021-03-05')
        self.assertEqual(self.f('更新履歴 2020/05/07'), '2020-05-07')

    def test_updated_english(self):
        self.assertEqual(self.f('Updated: 2020-05'), '2020-05-31')
        self.assertEqual(self.f('Last modified 2018.11.02'), '2018-11-02')

    def test_era(self):
        self.assertEqual(self.f('最終更新 令和3年4月1日'), '2021-04-01')
        self.assertEqual(self.f('更新日 平成20年12月'), '2008-12-31')

    def test_latest_of_several(self):
        self.assertEqual(self.f('© 2015 ... 最終更新 2022年8月10日'), '2022-08-10')

    def test_future_and_ancient_ignored(self):
        self.assertIsNone(self.f('© 2031'))
        self.assertIsNone(self.f('© 1980'))
        self.assertIsNone(self.f('最終更新 2027年1月1日'))

    def test_current_year_rounds_to_today(self):
        self.assertEqual(self.f('© 2026'), '2026-11-18')

    def test_no_false_positive_on_plain_numbers(self):
        self.assertIsNone(self.f('2019年の旅行記。創業2004年の老舗。'))
        self.assertIsNone(self.f('アップデート情報はこちら'))
        self.assertIsNone(self.f(''))
        self.assertIsNone(self.f(None))

    def test_invalid_month(self):
        self.assertIsNone(self.f('最終更新 2021年13月'))


class FooterSignal(unittest.TestCase):
    def test_source_distinguishes_copyright_and_updated(self):
        self.assertEqual(fr.footer_signal('(c) 2019 Taro', TODAY), ('2019-12-31', 'footer_copyright'))
        self.assertEqual(fr.footer_signal('最終更新 2021年3月', TODAY), ('2021-03-31', 'footer_updated'))
        self.assertEqual(fr.footer_signal('© 2015 ... 最終更新 2018年8月10日', TODAY), ('2018-08-10', 'footer_updated'))
        self.assertEqual(fr.footer_signal('最終更新 2018年8月10日 © 2022', TODAY), ('2022-12-31', 'footer_copyright'))   # 新しい方を採る
        self.assertEqual(fr.footer_signal('© 2020 最終更新 2020年12月31日', TODAY), ('2020-12-31', 'footer_updated'))   # 同日は更新表記
        self.assertEqual(fr.footer_signal('nothing', TODAY), (None, None))

    def test_features_keep_the_source(self):
        import features
        f = features.extract('a.example', '<html><body><p>hello</p><footer>© 2019</footer></body></html>', {})
        self.assertEqual((f['freshness_signal_at'], f['freshness_source']), ('2019-12-31', 'footer_copyright'))


class HeaderDate(unittest.TestCase):
    def test_old_last_modified(self):
        self.assertEqual(fr.header_date({'last-modified': 'Tue, 05 Mar 2019 10:00:00 GMT', 'date': 'Wed, 18 Nov 2026 01:00:00 GMT'}, TODAY), '2019-03-05')

    def test_dynamic_page_is_dropped(self):
        self.assertIsNone(fr.header_date({'last-modified': 'Wed, 18 Nov 2026 00:59:00 GMT'}, TODAY))
        self.assertIsNone(fr.header_date({'last-modified': 'Tue, 17 Nov 2026 12:00:00 GMT'}, TODAY))

    def test_garbage_and_missing(self):
        self.assertIsNone(fr.header_date({'last-modified': 'yesterday'}, TODAY))
        self.assertIsNone(fr.header_date({}, TODAY))
        self.assertIsNone(fr.header_date({'last-modified': 'Thu, 01 Jan 1970 00:00:00 GMT'}, TODAY))


class SitemapFeed(unittest.TestCase):
    def test_sitemap_max_lastmod(self):
        xml = '<urlset><url><loc>a</loc><lastmod>2019-01-02</lastmod></url><url><lastmod>2021-06-30T12:00:00+09:00</lastmod></url></urlset>'
        self.assertEqual(fr.sitemap_date(xml, TODAY), '2021-06-30')
        self.assertIsNone(fr.sitemap_date('<urlset/>', TODAY))

    def test_rss(self):
        xml = '<rss><channel><lastBuildDate>Wed, 18 Nov 2026 00:00:00 GMT</lastBuildDate><item><pubDate>Mon, 03 Feb 2020 09:00:00 +0900</pubDate></item><item><pubDate>Sun, 01 Dec 2019 09:00:00 +0900</pubDate></item></channel></rss>'
        self.assertEqual(fr.feed_date(xml, TODAY), '2020-02-03')

    def test_atom(self):
        xml = '<feed><updated>2022-04-05T00:00:00Z</updated><entry><published>2021-01-01T00:00:00Z</published></entry></feed>'
        self.assertEqual(fr.feed_date(xml, TODAY), '2022-04-05')

    def test_feed_links(self):
        html = '<head><link rel="alternate" type="application/rss+xml" title="x" href="/feed.xml"><link rel="stylesheet" href="a.css"><link href="/atom?a=1&amp;b=2" type="application/atom+xml" rel="alternate"></head>'
        self.assertEqual(fr.feed_links(html), ['/feed.xml', '/atom?a=1&b=2'])
        self.assertEqual(fr.feed_links('<link rel="alternate" hreflang="en" href="/en/">'), [])


class CollectFreshness(unittest.TestCase):
    """fetcher.collect_freshness を偽の http_get / robots で検証（ネットワークなし）。"""

    class Robots:
        def __init__(self, deny=()):
            self.deny = deny; self.asked = []

        def check(self, origin, path):
            self.asked.append(path)
            return 'allowed', path not in self.deny, 0

    def run_it(self, pages, deny=(), html='<html></html>', headers=None):
        import fetcher
        calls = []

        def fake_get(url, max_body=0):
            calls.append(url)
            if url in pages:
                return 200, {'content-type': 'application/xml'}, pages[url].encode()
            return 404, {}, b''
        out = {'status': 200, 'final_url': 'https://a.example/', 'headers': headers or {}, 'body': html.encode()}
        rb = self.Robots(deny)
        from unittest import mock
        with mock.patch.object(fetcher, 'http_get', fake_get), mock.patch.object(fetcher.time, 'sleep', lambda s: None):
            return fetcher.collect_freshness(out, rb, TODAY), calls

    def test_declared_feed_wins_and_stops(self):
        html = '<link rel="alternate" type="application/rss+xml" href="/f.xml">'
        found, calls = self.run_it({'https://a.example/f.xml': '<rss><item><pubDate>Mon, 03 Feb 2020 09:00:00 +0900</pubDate></item></rss>'}, html=html)
        self.assertEqual(calls, ['https://a.example/f.xml'])
        self.assertIn(('2020-02-03', 'feed'), found)

    def test_max_two_requests_and_sitemap(self):
        found, calls = self.run_it({'https://a.example/sitemap.xml': '<urlset><lastmod>2018-02-03</lastmod></urlset>'})
        self.assertLessEqual(len(calls), 2)
        self.assertIn(('2018-02-03', 'sitemap'), found)

    def test_robots_denied_is_not_fetched(self):
        found, calls = self.run_it({}, deny=('/sitemap.xml', '/rss.xml'))
        self.assertEqual(calls, [])

    def test_robots_check_includes_the_query(self):
        html = '<link rel="alternate" type="application/rss+xml" href="/?feed=rss2">'
        rb = self.Robots(deny=('/?feed=rss2',))
        import fetcher
        from unittest import mock
        out = {'status': 200, 'final_url': 'https://a.example/', 'headers': {}, 'body': html.encode()}
        calls = []
        with mock.patch.object(fetcher, 'http_get', lambda u, max_body=0: (calls.append(u), (404, {}, b''))[1]), mock.patch.object(fetcher.time, 'sleep', lambda s: None):
            fetcher.collect_freshness(out, rb, TODAY)
        self.assertIn('/?feed=rss2', rb.asked)
        self.assertNotIn('https://a.example/?feed=rss2', calls)

    def test_unexpected_exception_keeps_the_top_page(self):
        import fetcher
        from unittest import mock
        top = {'error': None, 'status': 200, 'headers': {'content-type': 'text/html'}, 'body': b'<html></html>', 'final_url': 'https://a.example/'}

        class RB:
            def check(self, origin, path): return 'allowed', True, 0
        with mock.patch.object(fetcher, 'RobotsCache', lambda sleep: RB()), \
             mock.patch.object(fetcher, 'collect_freshness', side_effect=ValueError('boom')), \
             mock.patch.object(fetcher, '_follow', lambda out, url, robots, delay: dict(out, **top)), \
             mock.patch.object(fetcher, 'check_public_host', lambda h: None), \
             mock.patch.object(fetcher.time, 'sleep', lambda s: None):
            res = fetcher.fetch_site('a.example')
        self.assertEqual(res['status'], 200)
        self.assertEqual(res['freshness'], [])

    def test_offsite_feed_ignored(self):
        html = '<link rel="alternate" type="application/rss+xml" href="https://other.example/f.xml">'
        found, calls = self.run_it({}, html=html)
        self.assertNotIn('https://other.example/f.xml', calls)


class Latest(unittest.TestCase):
    def test_latest_and_source(self):
        self.assertEqual(fr.latest([(None, 'header'), ('2019-01-01', 'footer'), ('2021-05-05', 'feed')]), ('2021-05-05', 'feed'))
        self.assertEqual(fr.latest([('2020-01-01', 'header'), ('2020-01-01', 'footer')]), ('2020-01-01', 'header'))
        self.assertEqual(fr.latest([(None, 'header')]), (None, None))
        self.assertEqual(fr.latest([]), (None, None))


if __name__ == '__main__':
    unittest.main()
