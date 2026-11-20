"""プラットフォーム内の店舗/ユーザーページの決定論の除外(#181)。ネットワークなし。
実行: uv run python3 -m unittest tests.test_platform_pages"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / 'pipeline'))
import discover_wikidata as dw
import frontier, hostnames, run

P = 'platform_page'
REJECT = [
    'https://www.instagram.com/some_shop', 'https://instagram.com/some_shop/', 'https://www.instagram.com/p/AbC123/',
    'https://www.facebook.com/shop.name', 'https://m.facebook.com/shop', 'https://x.com/user', 'https://twitter.com/user/status/1',
    'https://tabelog.com/ishikawa/A1701/A170101/17000000/', 'https://s.tabelog.com/tokyo/', 'https://r.gnavi.co.jp/abc123/',
    'https://www.gurunavi.com/ja/abc', 'https://www.hotpepper.jp/strJ000000000/', 'https://retty.me/area/PRE13/ARE1/SUB101/100000000/',
    'https://ameblo.jp/someone/', 'https://note.com/someone', 'https://line.me/R/ti/p/@shop', 'https://lin.ee/abcdef',
    'https://linktr.ee/someone', 'https://lit.link/someone', 'https://maps.app.goo.gl/AbCdEf', 'https://g.page/shop-name',
    'https://WWW.INSTAGRAM.COM/Some_Shop',                        # 大文字のホスト
    'https://www.instagram.com/some_shop?hl=ja',                  # クエリ付きでもパスがあれば除外
    'https://www.instagram.com/ja/some_shop',                     # 言語パスの下のユーザー
    'https://www.instagram.com//some_shop',
    'www.instagram.com/some_shop', 'instagram.com/some_shop',     # スキーム無し(申請のキー形式)
    'https://www.instagram.com/jp2',                              # 言語コードに似た別名は本体のトップではない
]
PASS = [
    'https://www.instagram.com/', 'https://www.instagram.com', 'https://instagram.com', 'https://www.facebook.com/',
    'https://x.com/', 'https://twitter.com', 'https://tabelog.com/', 'https://www.gurunavi.com/', 'https://www.hotpepper.jp/',
    'https://note.com/', 'https://line.me/', 'https://linktr.ee/', 'https://ameblo.jp/',
    'https://www.instagram.com/ja/', 'https://www.instagram.com/en', 'https://www.instagram.com/JA-JP/', 'https://www.facebook.com/en-us',   # 言語パス程度
    'https://www.instagram.com/?hl=ja', 'https://www.instagram.com/#top', 'https://www.instagram.com/ja/?hl=en',   # クエリ・フラグメント
    'www.instagram.com', 'instagram.com/',
    'https://example.jp/shop', 'https://www.example.co.jp/', 'https://sample.ne.jp/~me/', 'https://someone.sakura.ne.jp/blog/', 'https://www.geocities.jp/me/',
    'https://foo.fc2.com/page', 'https://foo.jimdofree.com/x', 'https://foo.wixsite.com/shop', 'https://home.ne.jp/~user/',
    'https://notinstagram.com/shop', 'https://instagram.com.evil.jp/shop', 'https://myx.com/page', 'https://fx.com/page',   # 接尾辞は label 境界で比べる
    'https://wikipedia.org/wiki/x', '', None, 'https://[bad', 'ftp://127.0.0.1/x',
]


class PlatformPage(unittest.TestCase):
    def test_reject(self):
        for u in REJECT:
            self.assertEqual(hostnames.platform_page(u), P, u)

    def test_pass(self):
        for u in PASS:
            self.assertIsNone(hostnames.platform_page(u), u)

    def test_hosts_come_from_one_file(self):
        hosts = hostnames.blocklist('platform_pages.txt')['host']
        self.assertGreaterEqual(len(hosts), 15)
        for h in hosts:   # 一覧の全ホストが「パスあり=除外・トップ=通す」になる(表と実装がずれない)
            self.assertEqual(hostnames.platform_page(f'https://{h}/shop-x'), P, h)
            self.assertIsNone(hostnames.platform_page(f'https://{h}/'), h)

    def test_empty_host_list_rejects_nothing(self):   # 変異: 一覧が空なら何も除外しない(=規則が一覧に依存している)
        with mock.patch.object(hostnames, 'blocklist', return_value={'host': [], 'top_path': ['ja']}):
            self.assertIsNone(hostnames.platform_page('https://www.instagram.com/shop'))

    def test_path_judgement_is_not_inverted(self):   # 変異: パス判定の反転(トップを落とし店を通す)で落ちる
        self.assertEqual(hostnames.platform_page('https://www.instagram.com/shop'), P)
        self.assertIsNone(hostnames.platform_page('https://www.instagram.com/'))

    def test_top_path_table_is_used(self):
        with mock.patch.object(hostnames, 'blocklist', return_value={'host': ['instagram.com'], 'top_path': []}):
            self.assertEqual(hostnames.platform_page('https://www.instagram.com/ja/'), P)

    def test_overlap_with_platforms_txt_is_pinned(self):   # 片方からホストが消えて意図せず食い違ったら落ちる
        pages = set(hostnames.blocklist('platform_pages.txt')['host'])
        shared = set(hostnames.blocklist('platforms.txt')[''])
        self.assertEqual(pages & shared, {'ameblo.jp', 'note.com'})

    def test_service_own_paths_are_excluded_known_hole(self):   # 既知の穴(platform_pages.txt の先頭コメント)。変えるなら docs も直す
        for u in ('https://note.com/info', 'https://x.com/home', 'https://www.instagram.com/accounts/login', 'https://help.instagram.com/x',
                  'https://developers.facebook.com/docs', 'https://www.gnavi.co.jp/company/'):
            self.assertEqual(hostnames.platform_page(u), P, u)

    def test_idn_host_is_not_judged(self):   # normalize_host は非 ASCII ラベルを拒否して None(通過)。偽陰性のみで、取得前フィルタが invalid_host で落とす
        self.assertIsNone(hostnames.platform_page('https://日本.instagram.com/x'))
        self.assertEqual(hostnames.prefilter('日本.instagram.com', jp_only=False), 'invalid_host')

    def test_every_top_path_in_file_passes(self):   # 言語パスの正本は [top_path] 1 か所。テストは表を読んで全項目を回す(手で二重管理しない)
        tops = hostnames.blocklist('platform_pages.txt')['top_path']
        self.assertTrue({'ja', 'jp', 'en', 'ja-jp'} <= set(tops))   # 日本語サイトの最低限(これが消えたら落ちる)
        for lp in tops:
            self.assertIsNone(hostnames.platform_page(f'https://www.instagram.com/{lp}/'), lp)
            self.assertEqual(hostnames.platform_page(f'https://www.instagram.com/{lp}/shop'), P, lp)

    def test_ne_jp_and_hosting_not_listed(self):
        hosts = set(hostnames.blocklist('platform_pages.txt')['host'])
        for h in ('ne.jp', 'sakura.ne.jp', 'fc2.com', 'jimdo.com', 'wixsite.com', 'jugem.jp', 'hatenablog.com'):
            self.assertNotIn(h, hosts)


class Triage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = frontier.connect(Path(self.tmp.name) / 'f.sqlite'); self.addCleanup(self.db.close)

    def test_triage_rejects_platform_page_keeps_top_and_selfmade(self):
        for key, host, url in [('instagram.com/shop', 'instagram.com', 'https://instagram.com/shop'),
                               ('instagram.com', 'instagram.com', 'https://instagram.com/'),
                               ('tabelog.com/tokyo/x', 'tabelog.com', 'https://tabelog.com/tokyo/x'),
                               ('home.ne.jp/~me', 'home.ne.jp', 'https://home.ne.jp/~me'),
                               ('shop.example.jp', 'shop.example.jp', 'https://shop.example.jp/')]:
            frontier.add(self.db, key, host, url, 'request')
        run.triage(self.db)
        st = {k: (s, r) for k, s, r in self.db.execute('SELECT key, state, reject_reason FROM frontier')}
        self.assertEqual(st['instagram.com/shop'], ('rejected', P))
        self.assertEqual(st['tabelog.com/tokyo/x'], ('rejected', P))
        self.assertEqual(st['instagram.com'][0], 'queued')
        self.assertEqual(st['home.ne.jp/~me'][0], 'queued')
        self.assertEqual(st['shop.example.jp'][0], 'queued')

    def test_redo_reapplies(self):
        frontier.add(self.db, 'instagram.com/shop', 'instagram.com', 'https://instagram.com/shop', 'request')
        run.triage(self.db); run.triage(self.db, redo=True)
        self.assertEqual(self.db.execute("SELECT reject_reason FROM frontier").fetchone()[0], P)


class WikidataDropsPlatformPages(unittest.TestCase):
    def test_platform_url_dropped_before_host_collapse(self):
        import tempfile as tf
        from tests.test_discover_wikidata import ROW, result
        with tf.TemporaryDirectory() as d:
            db = frontier.connect(Path(d) / 'f.sqlite')
            cl = dw.Client(cache_dir=Path(d) / 'c', sleep=lambda s: None, http_get=lambda u: (200, {}, result(
                ('Q1', 'https://www.instagram.com/some_shrine', None), ('Q2', 'https://www.instagram.com/', None), ('Q3', 'https://shrine.example.jp/', None))), interval=5.0, clock=lambda: 0.0)
            out = dw.discover_wikidata(db, [ROW], cl, max_rows=100, page_size=100, listed=set())
            s = out['rows'][0]
            self.assertEqual((s['platform_page'], s['added']), (1, 2))
            db.close()


if __name__ == '__main__':
    unittest.main()
