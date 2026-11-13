import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / 'pipeline'))
import features, fetcher, hostnames


class Hosts(unittest.TestCase):
    def test_reversed(self):
        self.assertEqual(hostnames.from_reversed('jp.example.www'), 'www.example.jp')
        self.assertIsNone(hostnames.from_reversed('jp.exa_mple'))

    def test_prefilter(self):
        for host, want in [('www.city.kanazawa.ishikawa.jp', 'government'), ('example.go.jp', 'government'), ('foo.lg.jp', 'government'),
                           ('toyota.co.jp', 'major_org'), ('u.ac.jp', 'institution_domain'), ('blog.example.jp', 'subdomain'),
                           ('ameblo.jp', 'platform'), ('foo-clinic.jp', None), ('example.co.jp', None), ('sample-porn.jp', 'adult_host'),
                           ('example.com', 'not_jp'), ('hyuki.jp', None), ('www.hyuki.ne.jp', None), ('1.2.3.4', 'invalid_host')]:
            self.assertEqual(hostnames.prefilter(host), want, host)


class Links(unittest.TestCase):
    def test_non_jp_allowed_for_links(self):
        self.assertIsNone(hostnames.prefilter('hyuki.com', jp_only=False))
        self.assertEqual(hostnames.prefilter('foo.github.io', jp_only=False), 'platform')


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

    def test_ads(self):
        ads = '<script src="https://pagead2.googlesyndication.com/x.js"></script>' * 5
        self.assertEqual(self.judged(self.PAGE.replace('</body>', ads + '</body>')), ('rejected', 'ad_heavy'))


class Kind(unittest.TestCase):
    def test_site_kind(self):
        self.assertEqual(features.site_kind('x.or.jp', '', ''), '団体')
        self.assertEqual(features.site_kind('x.jp', 'NPO法人あいう', ''), '団体')
        self.assertEqual(features.site_kind('x.jp', '株式会社あいう', ''), '企業')
        self.assertEqual(features.site_kind('x.jp', '私のページ', ''), '不明')

    def test_small_business_not_rejected(self):
        f = features.extract('x.jp', '<html lang=ja><title>株式会社あいう 工務店</title>' + 'ご相談はお気軽に。' * 40, {})
        self.assertEqual(features.judge(f), ('candidate', None))


class Robots(unittest.TestCase):
    def test_token(self):
        import urllib.robotparser
        p = urllib.robotparser.RobotFileParser(); p.parse(['User-agent: MiseResearchBot', 'Disallow: /'])
        self.assertFalse(p.can_fetch(fetcher.ROBOT_TOKEN, 'https://x.jp/'))


if __name__ == '__main__':
    unittest.main()
