#!/usr/bin/env python3
"""名簿スクリプトのテスト。実行: uv run --with openpyxl python3 -m unittest scripts/test_public_bodies.py -v
ネットワークには出ない。コミット済み台帳（data/public-bodies/ledger.csv）の整合も検査する。"""
import csv, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import public_bodies_common as common
import public_bodies_diff as pdiff
import public_bodies_sql as psql
import public_bodies_resolve as pres
import public_bodies_reach as preach

LEDGER = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies' / 'ledger.csv'

def row(**kw):
    base = {'code': '010006', 'name_ja': 'A', 'url': 'https://a.example.jp/', 'host': 'a.example.jp', 'status': 'active',
            'status_date': '', 'successor_code': '', 'note': ''}
    base.update(kw); return base

class HostKey(unittest.TestCase):
    def test_normalize(self):
        for u in ('http://www.city.x.lg.jp/', 'https://city.x.lg.jp', 'HTTPS://WWW.City.X.lg.jp/index.html'):
            self.assertEqual(common.host_key(u), 'city.x.lg.jp')
        self.assertEqual(common.host_key('https://www2.city.x.lg.jp/'), 'www2.city.x.lg.jp')
    def test_clean_url(self):
        self.assertEqual(common.clean_url('http://a.jp/index.html'), 'http://a.jp/')
        for u, want in (('https://v.jp/gyosei/index.html/', 'https://v.jp/gyosei/'), ('http://a.jp/INDEX.HTM', 'http://a.jp/'),
                        ('http://a.jp/x/Index.php?lang=ja#top', 'http://a.jp/x/'), ('http://a.jp/default.aspx', 'http://a.jp/'),
                        ('http://a.jp', 'http://a.jp'), ('http://a.jp/index_ja.html', 'http://a.jp/index_ja.html'), (' http://a.jp/top/ ', 'http://a.jp/top/')):
            self.assertEqual(common.clean_url(u), want, u)
    def test_url_key_keeps_path(self):
        self.assertEqual(common.url_key('https://www.mlit.go.jp/'), 'mlit.go.jp')
        self.assertEqual(common.url_key('http://MLIT.go.jp/jtsb/index.html'), 'mlit.go.jp/jtsb')
        self.assertNotEqual(common.url_key('https://www.mlit.go.jp/jtsb/'), common.url_key('https://www.mlit.go.jp/'))

class Diff(unittest.TestCase):
    def kinds(self, o, n): return [r[0] for r in pdiff.diff({r['code']: r for r in o}, {r['code']: r for r in n})]
    def test_same(self): self.assertEqual(self.kinds([row()], [row()]), [])
    def test_host_change(self): self.assertEqual(self.kinds([row()], [row(url='https://b.example.jp/', host='b.example.jp')]), ['URL変更(host)'])
    def test_url_only_change(self):
        self.assertEqual(self.kinds([row(url='http://a.example.jp/')], [row(url='https://a.example.jp/')]), ['URL変更(url)'])
        self.assertEqual(self.kinds([row()], [row(url='https://a.example.jp/new/')]), ['URL変更(url)'])
    def test_rename_merge_new_vanish(self):
        self.assertEqual(self.kinds([row()], [row(name_ja='B')]), ['名称変更'])
        self.assertEqual(self.kinds([row()], [row(status='merged')]), ['廃止/合併'])
        self.assertEqual(self.kinds([], [row()]), ['新設'])
        self.assertEqual(self.kinds([row()], []), ['消失'])

class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.rows = list(csv.DictReader(open(LEDGER, encoding='utf-8')))
    def test_active_has_two_sources_and_matching_host(self):
        for r in self.rows:
            if r['status'] != 'active': continue
            # 案(c): J-LIS を数えないので、掲載対象は 出典1以上 + reach の 2 要素、掲載しない行政区は出典1以上
            self.assertGreaterEqual(len(r['sources'].split(';')), 1 if r['type'] == '行政区' else 2, r['code'])
            self.assertEqual(common.host_key(r['url']), r['host'], r['code'])
    def test_successor_exists_and_not_self(self):
        codes = {r['code'] for r in self.rows if r['status'] in ('active', 'unverified-reach')} | {r['code'] for r in csv.DictReader(open(LEDGER.with_name('review.csv'), encoding='utf-8'))}
        for r in self.rows:
            if r['status'] == 'active': continue
            for s in filter(None, r['successor_code'].split('|')):
                self.assertIn(s, codes, r['code'] + ' -> ' + s); self.assertNotEqual(s, r['code'])
    def test_successor_name_matches_reason(self):
        names = {r['code']: r['name_ja'] for r in self.rows}
        names.update({r['code']: r['name_ja'] for r in csv.DictReader(open(LEDGER.with_name('review.csv'), encoding='utf-8'))})
        for r in self.rows:
            if r['status'] != 'merged' or not r['successor_code']: continue
            if '合併後は' in r['note'] and len(r['note'].split('合併後は')[1].split(' ')[0]) > 0:
                dest = r['note'].split('合併後は')[1].split(' ')[0]
                self.assertIn(dest, [names[s] for s in r['successor_code'].split('|')], r['code'])
    def test_horonai_horokanai_one_to_one(self):
        by = {r['code']: r for r in self.rows}
        self.assertEqual(by['014397']['successor_code'], '014729')
        self.assertEqual(by['014885']['successor_code'], '015202')

def lrow(**kw):
    base = {'code': '011002', 'name_ja': "札幌市", 'name_en': 'Sapporo', 'type': '市', 'prefecture': '北海道', 'url': 'https://www.city.sapporo.jp/',
            'status': 'active', 'sources': 'wikidata@2026-11-13;e-gov@2026-11-12'}
    base.update(kw); return base
PREF = lrow(code='010006', name_ja='北海道', name_en='Hokkaido', type='都道府県', prefecture='北海道', url='https://www.pref.hokkaido.lg.jp/')

class Sql(unittest.TestCase):
    def test_insert_local_and_national(self):
        sql = psql.generate([PREF, lrow(), lrow(code='nat:abc', name_ja='人事院', name_en='', type='国の機関', prefecture='', url='https://www.jinji.go.jp/')])
        self.assertIn("'pb-011002'", sql); self.assertIn("1,'JP-01'", sql)
        self.assertIn('Official website of Sapporo, Hokkaido, Japan.', sql)
        self.assertIn("'pb-nat-abc'", sql); self.assertNotIn("'JP-nat", sql)
        self.assertIn("'2026-11-13T00:00:00Z',1", sql); self.assertIn('ADD COLUMN bulk_import', sql)
        self.assertNotIn('ADD COLUMN', psql.generate([PREF, lrow()], [PREF]))
        self.assertIn("NOT EXISTS (SELECT 1 FROM sites WHERE rtrim(", sql); self.assertIn("='city.sapporo.jp')", sql)
    def test_skips_wards_inactive_and_quotes(self):
        rows = [PREF, lrow(code='011011', type='行政区', name_ja='札幌市中央区'), lrow(code='012025', status='merged'), lrow(code='013001', name_ja="O'x")]
        sql = psql.generate(rows)
        self.assertNotIn('pb-011011', sql); self.assertNotIn('pb-012025', sql); self.assertIn("'O''x'", sql)
        self.assertIn('pb-011011', psql.generate(rows, include_wards=True))
    def test_since_diff(self):
        old = [PREF, lrow(), lrow(code='012025', name_ja='函館市', url='https://a.jp/')]
        new = [PREF, lrow(name_ja='札幌新市'), lrow(code='012025', name_ja='函館市', url='https://a.jp/', status='merged'), lrow(code='013005', name_ja='新市', url='https://n.jp/')]
        sql = psql.generate(new, old)
        self.assertIn("UPDATE sites SET url='https://www.city.sapporo.jp/',name='札幌新市'", sql)
        self.assertIn("DELETE FROM sites WHERE id='pb-012025'", sql)
        self.assertIn("INSERT INTO sites", sql); self.assertIn("'pb-013005'", sql)
        self.assertNotIn("INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code) SELECT 'pb-010006'", sql)
    def test_committed_migration_matches_ledger(self):
        mig = pathlib.Path(__file__).resolve().parent.parent / 'migrations' / '0015_public_bodies.sql'
        self.assertEqual(mig.read_text(encoding='utf-8'), psql.generate(psql.load(LEDGER)), '台帳を変えたら migration を再生成するか、適用済みなら --since で新しい migration を足す')
    def test_dedupe_by_host_and_path(self):
        a, b = lrow(), lrow(code='011003', url='http://WWW.city.sapporo.jp/index.html/')
        self.assertEqual(psql.norm(a['url']), psql.norm(b['url']))
        self.assertIn("rtrim(replace(replace(replace(lower(url)", psql.generate([PREF, a]))
    def test_category_added_to_existing_by_normalized_url(self):
        self.assertIn("SELECT id,'government-government-agencies' FROM sites WHERE rtrim(", psql.generate([PREF, lrow()]))
    def test_ledger_ids_and_urls_unique(self):
        listed = psql.listed(psql.load(LEDGER), False).values()
        listed = list(listed)
        self.assertEqual(len({psql.norm(r['url']) for r in listed}), len(listed))
        for r in listed: self.assertEqual(common.clean_url(r['url']), r['url'], r['code'])
    # 掲載 URL に J-LIS 由来のパスが無いこと。J-LIS の URL は照合にだけ使い、掲載するのは他出典かサイト自身が返した URL（docs/collection/public-bodies.md）。
    # 自治体のトップは `/` が原則。パスがあるのは、Wikidata の公式サイト(P856)自身がそのパスを持つ次の2件だけ（raw の wikidata-p429.csv で確認済み）。
    WIKIDATA_DEEP = {'015199': '/rishirifuji', '262137': '/www/'}
    def test_no_jlis_derived_paths_in_listed_urls(self):
        import urllib.parse
        for r in csv.DictReader(open(LEDGER, encoding='utf-8')):
            if r['status'] != 'active' or r['type'] not in ('都道府県', '市', '町', '村', '特別区'): continue
            path = urllib.parse.urlsplit(r['url']).path
            self.assertEqual(path if path else '/', self.WIKIDATA_DEEP.get(r['code'], '/'), r['code'] + ' ' + r['url'])
    def test_listed_basis_is_non_jlis_source_plus_reach(self):
        # 案(c): J-LIS は出典に数えない。掲載行は Wikidata か e-Gov のどちらかと、到達確認(reach)を sources に持つ。
        for r in csv.DictReader(open(LEDGER, encoding='utf-8')):
            names = {x.split('@')[0] for x in r['sources'].split(';') if x}
            self.assertFalse([n for n in names if n.startswith('j-lis')], r['code'] + ' sources に j-lis')
            if r['status'] == 'active' and r['type'] in ('都道府県', '市', '町', '村', '特別区', '国の機関', '独立行政法人'):
                self.assertTrue(names & {'wikidata', 'e-gov', 'override'}, r['code'] + ' に非J-LIS出典が無い')
                self.assertIn('reach', names, r['code'])
    def test_credit_does_not_cite_jlis(self):
        root = LEDGER.parent.parent.parent
        for f in ('public/app.js', 'public/index.html'):
            self.assertNotIn('J-LIS', (root / f).read_text(encoding='utf-8'), f)
    def test_listed_rows_have_ok_reachability(self):
        ev = {e['code']: e for e in csv.DictReader(open(LEDGER.with_name('evidence') / 'reachability.csv', encoding='utf-8'))}
        for r in psql.listed(psql.load(LEDGER), False).values():
            e = ev.get(r['code'])
            self.assertIsNotNone(e, r['code'] + ' に到達確認が無い'); self.assertEqual(e['verdict'], 'ok', r['code'])
            self.assertEqual(common.host_key(r['url']), common.host_key(e['final_url']), r['code'])
        for r in psql.load(LEDGER):
            if r['status'] == 'unverified-reach': self.assertNotIn(r['code'], psql.listed(psql.load(LEDGER), False))
    def test_national_urls_match_wikidata_by_path(self):
        ev = LEDGER.with_name('evidence') / 'wikidata-gojp.csv'
        keys = {common.url_key(e['site']) for e in csv.DictReader(open(ev, encoding='utf-8'))}
        for r in csv.DictReader(open(LEDGER, encoding='utf-8')):
            if r['code'].startswith('nat:') and r['status'] == 'active':
                # 到達確認で最終 URL に置換された行は note に旧→新が残る。それ以外は台帳の URL がそのまま Wikidata の P856 にある
                self.assertTrue(common.url_key(r['url']) in keys or 'URL移転' in r['note'], r['name_ja'])

class Reach(unittest.TestCase):
    ROW = {'host': 'city.x.lg.jp'}
    def v(self, **kw):
        a = dict(robots_ok=True, exit_code=0, status=200, final='https://www.city.x.lg.jp/', title='X市')
        a.update(kw); return preach.verdict(self.ROW, **a)
    def test_ok_and_moves(self):
        self.assertEqual(self.v(), 'ok')
        self.assertEqual(self.v(final='https://www.town.y.lg.jp/'), 'ok')   # 公的ドメインへの移転
        self.assertEqual(self.v(exit_code=63), 'ok')                       # サイズ上限で打ち切り
    def test_not_ok(self):
        self.assertEqual(self.v(final='https://shop.example.com/'), 'other-org')
        self.assertEqual(self.v(exit_code=6, status=0, final=''), 'dead')
        self.assertEqual(self.v(status=404), 'dead'); self.assertEqual(self.v(status=503), 'dead')
        self.assertEqual(self.v(exit_code=28, status=0), 'unverified'); self.assertEqual(self.v(status=403), 'unverified')
        self.assertEqual(self.v(robots_ok=False), 'unverified')
        self.assertEqual(self.v(title='This domain is for sale'), 'parked')

class Resolve(unittest.TestCase):
    R = {'code': '1', 'wikidata_urls': 'https://new.example.lg.jp/', 'egov_url': '', 'jlis_url': 'http://old.example.jp/'}
    def ev(self, **kw):
        e = {'code': '1', 'source': 'jlis', 'from_url': 'http://old.example.jp/', 'robots': 'allowed', 'http_status': '200',
             'final_url': 'https://www.new.example.lg.jp/', 'checked_at': '2026-11-14T00:00:00Z'}
        e.update(kw); return [e]
    def test_promote_when_redirect_lands_on_other_source_host(self):
        self.assertEqual(pres.promotable(self.R, self.ev()), ('https://new.example.lg.jp/', 'jlis', 'wikidata', '2026-11-14'))
    def test_not_promoted_otherwise(self):
        for kw in ({'final_url': 'https://other.example.jp/'}, {'http_status': '503'}, {'robots': 'disallowed'}, {'final_url': 'http://old.example.jp/'}):
            self.assertIsNone(pres.promotable(self.R, self.ev(**kw)), kw)

if __name__ == '__main__': unittest.main()
