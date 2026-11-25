"""葉に狙いを定めた URL 発見(scripts/discover_leaf_directed.py)。ネットワーク・本番 D1 には出ない(D1 は実 migration の DDL で作る in-memory SQLite)。"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
sys.path.insert(0, str(ROOT / 'tests'))
import discover_leaf_directed as dld
import discover_online as do
import discover_wikidata as dw
from test_discover_online import FakeD1, FakeWdqs, serve

MIG = ROOT / 'migrations'
CANDIDATES_DDL = (MIG / '0036_url_candidates.sql').read_text(encoding='utf-8')
INIT = (MIG / '0001_initial.sql').read_text(encoding='utf-8')
LEAF_DDL = '\n'.join(re.search(rf'CREATE TABLE {t} \(.*?\);', INIT).group(0) for t in ('categories', 'site_categories'))   # 実 migration の DDL
MAP = {r['leaf_id']: r for r in dw.load_map()}
QID = {l: MAP[l]['qid'] for l in ('shrines', 'temples', 'libraries', 'museums')}


def leaf_rows(*leaves, max_rows='5000'):
    seen, out = set(), []
    for r in dw.load_map():
        if r['leaf_id'] in leaves and r['leaf_id'] not in seen:   # 葉ごとに先頭の 1 行だけ
            seen.add(r['leaf_id'])
            out.append(dict(r, max_rows=max_rows))
    return out


class LeafD1(FakeD1):
    """FakeD1 + 実 migration の categories / site_categories / url_candidates。"""
    def __init__(self, counts, **kw):
        super().__init__(**kw)
        self.db.executescript(LEAF_DDL)
        self.db.executescript(CANDIDATES_DDL)
        self.db.execute("INSERT INTO categories VALUES ('reference',NULL,'調べもの','Reference',0)")
        for leaf in counts:
            self.db.execute('INSERT INTO categories VALUES (?,?,?,?,0)', (leaf, 'reference', f'名前-{leaf}', leaf))
        for leaf, n in counts.items():
            for i in range(n):
                sid = f'{leaf}-{i:03d}'
                self.db.execute('INSERT INTO sites(id,url,progress_key) VALUES(?,?,?)', (sid, f'https://{leaf}{i}.example.jp/', f'{leaf}{i}.example.jp'))
                self.db.execute('INSERT INTO site_categories VALUES (?,?)', (sid, leaf))
        self.db.commit()

    def hint(self, key):
        r = self.db.execute('SELECT hint_leaf FROM url_candidates WHERE identity_key=?', (key,)).fetchone()
        return r[0] if r else None

    def table_count(self, t):
        return self.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]


def run_leaves(d1, items, rows, apply=True, limit=None, max_rows=5000, **kw):
    f = items if isinstance(items, FakeWdqs) else FakeWdqs(items)
    with mock.patch.object(dld, 'LEAF_PAGE', kw.pop('page', dld.LEAF_PAGE)):
        out = []
        code, stats = do.run(d1, ('wikidata-leaves',), apply, limit, max_rows, wdqs=f.client(), leaf_rows=rows, out=out.append, **kw)
    return code, stats, f, out


class Deficit(unittest.TestCase):
    def test_deficit_counts_only_leaves_and_floor_is_100(self):
        d1 = LeafD1({'shrines': 20, 'film': 100, 'temples': 130})
        st = dld.leaf_state(d1)
        self.assertEqual(dld.FLOOR, 100)
        self.assertNotIn('reference', st)   # 子を持つカテゴリは葉ではない
        self.assertEqual({l: s['deficit'] for l, s in st.items()}, {'shrines': 80, 'film': 0, 'temples': 0})
        self.assertEqual(dld.plan(st), ['shrines'])

    def test_deficit_descending_order_ties_by_leaf_id(self):
        d1 = LeafD1({'libraries': 90, 'shrines': 20, 'temples': 20, 'museums': 50})
        self.assertEqual(dld.plan(dld.leaf_state(d1)), ['shrines', 'temples', 'museums', 'libraries'])

    def test_wikidata_leaves_processed_in_deficit_order_and_skip_full_leaf(self):
        d1 = LeafD1({'libraries': 90, 'shrines': 20, 'temples': 50, 'museums': 100})
        f = FakeWdqs({'Q1': 'https://a.example.jp/'})
        run_leaves(d1, f, leaf_rows('libraries', 'shrines', 'temples', 'museums'))
        firsts = [re.search(r'wd:(Q\d+) ;', c).group(1) for c in f.calls if 'LIMIT' in c and 'VALUES' not in c]
        self.assertEqual(firsts, [QID['shrines'], QID['temples'], QID['libraries']])   # museums は不足 0 なので問い合わせない


class Tagging(unittest.TestCase):
    ITEMS = {'Q1': 'https://tanaka-taro.jp/', 'Q2': 'https://hanako.hatenablog.com/', 'Q3': 'https://yamada.example.net/~me/', 'Q4': 'https://www.facebook.com/x'}

    def test_new_rows_get_progress_and_hint_and_personal_domains_pass(self):
        d1 = LeafD1({'shrines': 20})
        code, stats, _, _ = run_leaves(d1, self.ITEMS, leaf_rows('shrines'))
        self.assertEqual((code, stats.mismatch), (0, 0))
        self.assertEqual(d1.hint('tanaka-taro.jp/'), 'shrines')
        self.assertEqual(d1.hint('hanako.hatenablog.com/'), 'shrines')
        self.assertIsNone(d1.hint('www.facebook.com/x/'))
        c = stats.leaves['shrines']
        self.assertEqual((c['deficit'], c['fetched'], c['added'], c['known'], c['tagged']), (80, 4, 3, 0, 0))   # SNS は入口として除外
        row = d1.db.execute("SELECT source,state FROM url_progress WHERE identity_key='tanaka-taro.jp/'").fetchone()
        self.assertEqual(tuple(row), ('discovered', 'discovered'))
        self.assertEqual(d1.db.execute("SELECT source FROM url_candidates WHERE identity_key='tanaka-taro.jp/'").fetchone()[0], 'wikidata-leaves')

    def test_hint_never_overwritten_and_fetch_columns_untouched(self):
        d1 = LeafD1({'shrines': 20})
        d1.db.execute("INSERT INTO url_progress(identity_key,url) VALUES('tanaka-taro.jp/','https://tanaka-taro.jp/')")
        d1.db.execute("INSERT INTO url_candidates(identity_key,url,hint_leaf,title,state,fetch_attempts) VALUES('tanaka-taro.jp/','https://tanaka-taro.jp/','temples','T','fetched',2)")
        d1.db.commit()
        run_leaves(d1, self.ITEMS, leaf_rows('shrines'))
        r = d1.db.execute("SELECT hint_leaf,title,state,fetch_attempts FROM url_candidates WHERE identity_key='tanaka-taro.jp/'").fetchone()
        self.assertEqual(tuple(r), ('temples', 'T', 'fetched', 2))
        # SQL 自体が COALESCE で守っている(別の葉で直接 upsert しても変わらない)
        sink = do.Sink(d1, True, do.Stats())
        sink.offer('x', 'c', 'https://tanaka-taro.jp/', hint_leaf='libraries')
        sink.flush()
        self.assertEqual(d1.hint('tanaka-taro.jp/'), 'temples')

    def test_known_discovered_row_without_hint_is_tagged_and_counted_separately(self):
        d1 = LeafD1({'shrines': 20})
        for k in ('tanaka-taro.jp/', 'hanako.hatenablog.com/'):
            d1.db.execute("INSERT INTO url_progress(identity_key,url,state) VALUES(?,?,?)", (k, 'https://' + k, 'discovered' if k.startswith('tanaka') else 'fetched'))
        d1.db.commit()
        _, stats, _, _ = run_leaves(d1, self.ITEMS, leaf_rows('shrines'))
        c = stats.leaves['shrines']
        self.assertEqual((c['known'], c['tagged'], c['added']), (2, 1, 1))
        self.assertEqual(d1.hint('tanaka-taro.jp/'), 'shrines')
        self.assertIsNone(d1.hint('hanako.hatenablog.com/'))   # discovered 以外には付けない
        self.assertEqual(d1.db.execute("SELECT state FROM url_progress WHERE identity_key='tanaka-taro.jp/'").fetchone()[0], 'discovered')

    def test_published_site_is_known_and_never_tagged(self):
        d1 = LeafD1({'shrines': 20})
        d1.db.execute("UPDATE sites SET progress_key='tanaka-taro.jp' WHERE id='shrines-000'")
        d1.db.commit()
        _, stats, _, _ = run_leaves(d1, {'Q1': 'https://tanaka-taro.jp/'}, leaf_rows('shrines'))
        self.assertEqual((stats.leaves['shrines']['known'], stats.leaves['shrines']['added'], stats.leaves['shrines']['tagged']), (1, 0, 0))
        self.assertIsNone(d1.hint('tanaka-taro.jp/'))


class Cap(unittest.TestCase):
    def test_stops_when_published_plus_hinted_reaches_3x_floor(self):
        d1 = LeafD1({'shrines': 90})
        items = {f'Q{n}': f'https://s{n}.example.jp/' for n in range(1, 1001)}
        _, stats, f, _ = run_leaves(d1, items, leaf_rows('shrines'), page=20)
        added = stats.leaves['shrines']['added']
        self.assertGreaterEqual(90 + added, 300)            # 公開 90 + 取り込み >= 300 で止まる
        self.assertLess(added, 210 + 20)                    # 行き過ぎは 1 ページ(ここでは 20)未満
        self.assertEqual(d1.table_count('url_candidates'), added)
        self.assertLess(len([c for c in f.calls if 'VALUES' not in c]), 14)   # 止まったあとは問い合わせない

    def test_existing_hinted_rows_count_toward_the_cap(self):
        d1 = LeafD1({'shrines': 90})
        for i in range(210):
            d1.db.execute('INSERT INTO url_progress(identity_key,url) VALUES(?,?)', (f'old{i}.jp/', f'https://old{i}.jp/'))
            d1.db.execute("INSERT INTO url_candidates(identity_key,url,hint_leaf) VALUES(?,?,'shrines')", (f'old{i}.jp/', f'https://old{i}.jp/'))
        d1.db.commit()
        _, stats, f, _ = run_leaves(d1, {'Q1': 'https://new.example.jp/'}, leaf_rows('shrines'))
        self.assertEqual(f.calls, [])   # すでに 300 に届いている葉は問い合わせもしない
        self.assertEqual(stats.leaves['shrines']['added'], 0)

    def test_rejected_and_published_hinted_rows_do_not_count(self):
        d1 = LeafD1({'shrines': 90})
        for i, st in enumerate(['rejected', 'failed', 'blocked', 'published']):
            d1.db.execute('INSERT INTO url_progress(identity_key,url,state) VALUES(?,?,?)', (f'old{i}.jp/', f'https://old{i}.jp/', st))
            d1.db.execute("INSERT INTO url_candidates(identity_key,url,hint_leaf) VALUES(?,?,'shrines')", (f'old{i}.jp/', f'https://old{i}.jp/'))
        d1.db.commit()
        self.assertEqual(dld.leaf_state(d1)['shrines']['hinted'], 0)


class Cursors(unittest.TestCase):
    ITEMS = {f'Q{n}': f'https://s{n}.example.jp/' for n in range(1, 8)}

    def cursor(self, d1):
        r = d1.db.execute("SELECT cursor_value FROM discovery_cursors WHERE source='wikidata-leaves'").fetchall()
        return [x[0] for x in r]

    def test_split_runs_resume_from_d1_cursor_and_finish_once(self):
        d1 = LeafD1({'shrines': 20})
        rows = leaf_rows('shrines')
        for _ in range(4):
            run_leaves(d1, self.ITEMS, rows, max_rows=3, page=2)
        self.assertEqual(d1.count(), 7)
        self.assertEqual(self.cursor(d1), ['done'])
        _, _, f, _ = run_leaves(d1, self.ITEMS, rows)
        self.assertEqual(f.calls, [])   # 取り終えた行はネットワークに出ない

    def test_row_max_rows_is_honoured(self):
        d1 = LeafD1({'shrines': 20})
        run_leaves(d1, self.ITEMS, leaf_rows('shrines', max_rows='4'), page=2)
        self.assertEqual(d1.count(), 4)
        self.assertEqual(self.cursor(d1), ['Q4'])

    def test_invalid_cursor_restarts_and_never_reaches_query(self):
        d1 = LeafD1({'shrines': 20})
        d1.db.execute("INSERT INTO discovery_cursors(source,cursor_key,cursor_value) VALUES('wikidata-leaves',?,'x\" ) } #')", (dw.cursor_name(leaf_rows('shrines')[0]),))
        d1.db.commit()
        _, stats, f, _ = run_leaves(d1, {'Q1': 'https://a.example.jp/'}, leaf_rows('shrines'))
        self.assertTrue(all('#' not in c for c in f.calls))
        self.assertEqual(d1.count(), 1)

    def test_429_exits_3_and_keeps_cursor(self):
        d1 = LeafD1({'shrines': 20})
        code, _, _, _ = run_leaves(d1, FakeWdqs({'Q1': 'https://a.example.jp/'}, statuses=[429]), leaf_rows('shrines'))
        self.assertEqual(code, do.WDQS_ABORT_EXIT)
        self.assertEqual(self.cursor(d1), [])


class Safety(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        d1 = LeafD1({'shrines': 20})
        d1.db.execute("INSERT INTO url_progress(identity_key,url) VALUES('tanaka-taro.jp/','https://tanaka-taro.jp/')")
        d1.db.commit()
        before = {t: d1.table_count(t) for t in ('url_progress', 'url_candidates', 'discovery_cursors')}
        _, stats, _, out = run_leaves(d1, Tagging.ITEMS, leaf_rows('shrines'), apply=False)
        self.assertEqual({t: d1.table_count(t) for t in before}, before)
        self.assertEqual(stats.leaves['shrines']['added'], 2)       # 追加予定
        self.assertEqual(stats.leaves['shrines']['tagged'], 1)      # 付ける予定
        self.assertTrue(any(l.startswith('shrines') for l in out))  # 葉ごとの表が出る
        self.assertFalse(any(re.match(r'\s*(INSERT|UPDATE|DELETE)', s) for s in d1.statements if 'url_candidates' in s))

    def test_params_never_exceed_100_with_many_rows(self):
        d1 = LeafD1({'shrines': 20})
        items = {f'Q{n}': f'https://site{n}.example.jp/' for n in range(1, 401)}
        _, stats, _, _ = run_leaves(d1, items, leaf_rows('shrines'))
        self.assertEqual((d1.count(), d1.table_count('url_candidates'), stats.mismatch), (280, 280, 0))   # 公開 20 + 280 = 300 で止まる
        self.assertLessEqual(d1.max_params, 100)

    def test_source_creates_no_files(self):
        text = (ROOT / 'scripts' / 'discover_leaf_directed.py').read_text(encoding='utf-8')
        imports = ' '.join(re.findall(r'^(?:import|from)\s+(\w+)', text, re.M)).split()
        for mod in ['sqlite3', 'csv', 'tempfile', 'shutil', 'pickle', 'shelve', 'gzip']:
            self.assertNotIn(mod, imports, mod)
        for token in [' open(', 'write_text', 'write_bytes', 'mkdir', '.jsonl']:
            self.assertNotIn(token, text, token)
        for token in ['sitelinks', 'popular', 'ORDER BY COUNT', 'rank']:
            self.assertNotIn(token, text, token)   # 人気・規模で選ばない

    def test_query_is_keyset_without_popularity_filter(self):
        q = dld.build_ids_query(MAP['shrines'], 200, 'Q400')
        self.assertIn('FILTER(STR(?item) > "http://www.wikidata.org/entity/Q400")', q)
        self.assertIn('ORDER BY STR(?item) LIMIT 200', q)
        for bad in ('OFFSET', 'sitelinks', 'P576'):
            self.assertNotIn(bad, q)


TOP = '<html><body><a href="{u}">link</a><a href="https://www.facebook.com/x">sns</a></body></html>'


def pf():
    return do.PoliteFetcher(sleep=lambda s: None, wait=0)


class LinksLeaf(unittest.TestCase):
    def setUp(self):
        self.d1 = LeafD1({'shrines': 10, 'museums': 5, 'film': 100})
        self.pages = {}
        for r in self.d1.db.execute('SELECT id,url FROM sites').fetchall():
            self.pages[r['url']] = TOP.format(u=f'https://out-{r["id"]}.example.org/')

    def run_links(self, apply=True, max_sites=100, limit=None):
        with mock.patch.object(do.fetcher, 'http_get', serve(self.pages)):
            return do.run(self.d1, ('links',), apply, limit, pf=pf(), out=lambda *_: None, extra={'max_sites': max_sites, 'leaf_seeds': True})

    def cursor(self, leaf):
        r = self.d1.db.execute("SELECT cursor_value FROM discovery_cursors WHERE source='links-leaf' AND cursor_key=?", (leaf,)).fetchone()
        return r[0] if r else None

    def test_seeds_by_deficit_tag_hint_skip_zero_deficit(self):
        _, stats = self.run_links(max_sites=7)
        hints = dict(self.d1.db.execute('SELECT identity_key,hint_leaf FROM url_candidates').fetchall())
        museums = {k for k, h in hints.items() if h == 'museums'}
        self.assertEqual(len(museums), 5)                                    # 不足 95 の museums を先に全シード
        self.assertEqual(sum(1 for h in hints.values() if h == 'shrines'), 2)  # 残り 2 サイトを shrines(不足 90)から
        self.assertNotIn('film', hints.values())                             # 不足 0 の葉のサイトはシードにしない
        self.assertTrue(all(h in ('museums', 'shrines') for h in hints.values()))
        self.assertIn('out-museums-000.example.org/', hints)
        self.assertNotIn('www.facebook.com/x/', hints)
        self.assertEqual((self.cursor('museums'), self.cursor('shrines'), self.cursor('film')), ('museums-004', 'shrines-001', None))
        self.assertEqual(stats.leaves['museums']['added'], 5)
        self.assertEqual(self.d1.count(), 7)

    def test_resume_per_leaf_from_d1_and_dry_run_writes_nothing(self):
        self.run_links(max_sites=7)
        self.run_links(max_sites=3)
        self.assertEqual(self.cursor('shrines'), 'shrines-004')   # 前回の続きから
        before = (self.d1.count(), self.d1.table_count('url_candidates'), self.d1.table_count('discovery_cursors'))
        self.run_links(apply=False)
        self.assertEqual((self.d1.count(), self.d1.table_count('url_candidates'), self.d1.table_count('discovery_cursors')), before)

    def test_known_link_target_without_hint_is_tagged(self):
        self.d1.db.execute("INSERT INTO url_progress(identity_key,url) VALUES('out-museums-000.example.org/','https://out-museums-000.example.org/')")
        self.d1.db.commit()
        _, stats = self.run_links(max_sites=1)
        self.assertEqual((stats.leaves['museums']['known'], stats.leaves['museums']['tagged'], stats.leaves['museums']['added']), (1, 1, 0))
        self.assertEqual(self.d1.hint('out-museums-000.example.org/'), 'museums')

    def test_default_off_keeps_id_order_mode(self):
        with mock.patch.object(do.fetcher, 'http_get', serve(self.pages)):
            do.run(self.d1, ('links',), True, None, pf=pf(), out=lambda *_: None, extra={'max_sites': 1})
        self.assertEqual(self.d1.table_count('url_candidates'), 0)   # run_sources 単体の既定は従来の id 昇順(hint なし)
        self.assertIsNotNone(self.d1.db.execute("SELECT 1 FROM discovery_cursors WHERE source='links' AND cursor_key='sites_id'").fetchone())


class Unmapped(unittest.TestCase):
    def test_lists_only_unmapped_deficit_leaves_without_urls(self):
        d1 = LeafD1({'poetry': 12, 'folklore': 40, 'shrines': 5, 'film': 100})
        out = []
        todo = dld.report_unmapped(d1, dw.load_map(), out.append)
        self.assertEqual(todo, ['poetry', 'folklore'])   # shrines は対応あり、film は不足 0
        text = '\n'.join(out)
        self.assertNotIn('http', text)
        self.assertIn('リンク集', text)
        self.assertIn('ウェブリング', text)
        self.assertIn('88', text)


if __name__ == '__main__':
    unittest.main()
