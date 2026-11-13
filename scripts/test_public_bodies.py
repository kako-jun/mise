#!/usr/bin/env python3
"""名簿スクリプトのテスト。実行: uv run --with openpyxl python3 -m unittest scripts/test_public_bodies.py -v
ネットワークには出ない。コミット済み台帳（data/public-bodies/ledger.csv）の整合も検査する。"""
import csv, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import public_bodies_common as common
import public_bodies_diff as pdiff
import public_bodies_sql as psql
import public_bodies_resolve as pres

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
            self.assertGreaterEqual(len(r['sources'].split(';')), 2, r['code'])
            self.assertEqual(common.host_key(r['url']), r['host'], r['code'])
    def test_successor_exists_and_not_self(self):
        codes = {r['code'] for r in self.rows if r['status'] == 'active'} | {r['code'] for r in csv.DictReader(open(LEDGER.with_name('review.csv'), encoding='utf-8'))}
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
            'status': 'active'}
    base.update(kw); return base
PREF = lrow(code='010006', name_ja='北海道', name_en='Hokkaido', type='都道府県', prefecture='北海道', url='https://www.pref.hokkaido.lg.jp/')

class Sql(unittest.TestCase):
    def test_insert_local_and_national(self):
        sql = psql.generate([PREF, lrow(), lrow(code='nat:abc', name_ja='人事院', name_en='', type='国の機関', prefecture='', url='https://www.jinji.go.jp/')])
        self.assertIn("'pb-011002'", sql); self.assertIn("'JP','JP-01'", sql)
        self.assertIn('Official website of Sapporo, Hokkaido, Japan.', sql)
        self.assertIn("'pb-nat-abc'", sql); self.assertNotIn("'JP-nat", sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM sites WHERE url='https://www.city.sapporo.jp/')", sql)
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
    def test_ledger_ids_and_urls_unique(self):
        listed = psql.listed(psql.load(LEDGER), False).values()
        self.assertEqual(len({r['url'] for r in listed}), len(list(listed)))

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
