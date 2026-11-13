#!/usr/bin/env python3
"""名簿スクリプトのテスト。実行: uv run --with openpyxl python3 -m unittest scripts/test_public_bodies.py -v
ネットワークには出ない。コミット済み台帳（data/public-bodies/ledger.csv）の整合も検査する。"""
import csv, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import public_bodies_common as common
import public_bodies_diff as pdiff

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

if __name__ == '__main__': unittest.main()
