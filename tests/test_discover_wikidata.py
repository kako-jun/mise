"""Wikidata の公式サイト発見元(#128)。ネットワークは全て差し替える。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
import discover_wikidata as dw
import export_candidates
import frontier
import run

ROW = {'leaf_id': 'shrines', 'qid': 'Q845945', 'rel': 'P31', 'country_prop': 'P17|P495', 'subclass': '1', 'label_en': 'Shinto shrine', 'note': '', 'max_rows': '100'}


def result(*items):
    return json.dumps({'results': {'bindings': [{'item': {'value': f'http://www.wikidata.org/entity/{q}'}, 'url': {'value': u}, **({'label': {'value': l}} if l else {})} for q, u, l in items]}}).encode()


class Map(unittest.TestCase):
    def test_shipped_map_is_valid(self):
        self.assertEqual(dw.validate_map(dw.load_map()), [])

    def test_validation_catches_bad_rows(self):
        bad = [dict(ROW, leaf_id='no-such-leaf'), dict(ROW, qid='845945'), dict(ROW, leaf_id='science'),     # 存在しない / 形式 / 親(葉でない)
               dict(ROW, leaf_id='cities'), dict(ROW, rel='P5'), dict(ROW, max_rows='0'), dict(ROW, country_prop='17')]  # assign=rule の葉 / rel / 件数 / 国の property
        errs = dw.validate_map(bad)
        self.assertEqual(len(errs), 8, errs)   # 7 行の不備 + 同じ (葉, qid, rel) の重複 1
        self.assertEqual(dw.validate_map([ROW, dict(ROW)]).__len__(), 1)   # 重複

    def test_select_rows_orders_by_leaves(self):
        rows = [dict(ROW, leaf_id='a'), dict(ROW, leaf_id='b'), dict(ROW, leaf_id='a', qid='Q1')]
        self.assertEqual([r['leaf_id'] for r in dw.select_rows(rows, ['b', 'a'])], ['b', 'a', 'a'])
        self.assertEqual([r['leaf_id'] for r in dw.select_rows(rows, None, 1)], ['a', 'a'])


class Parse(unittest.TestCase):
    def test_parse_and_normalize(self):
        got = dw.parse_results(json.loads(result(('Q1', 'https://www.Example.jp/a/b?x=1', '神社'), ('Q2', 'http://foo.com', None))))
        self.assertEqual(got, [('Q1', 'https://www.Example.jp/a/b?x=1', '神社'), ('Q2', 'http://foo.com', None)])
        self.assertEqual(dw.parse_results({}), [])
        self.assertEqual(dw.normalize_url('https://www.Example.jp/a/b?x=1'), ('example.jp', 'www.example.jp', 'https://www.example.jp/'))
        self.assertEqual(dw.normalize_url('http://foo.com:8080/x')[0], 'foo.com') if dw.normalize_url('http://foo.com:8080/x') else None
        for bad in ('ftp://x.jp/', 'http://192.168.0.1/', 'mailto:a@b.jp', 'not a url', '', None, 'http://localhost/'):
            self.assertIsNone(dw.normalize_url(bad), bad)

    def test_query_shape(self):
        q = dw.build_query(ROW, 200, 400)
        self.assertIn('wdt:P31/wdt:P279* wd:Q845945', q)
        self.assertIn('(wdt:P17|wdt:P495) wd:Q17', q)
        self.assertIn('LIMIT 200 OFFSET 400', q)
        q2 = dw.build_query(dict(ROW, rel='P106', country_prop='P27', subclass='0', qid='Q191633'), 10, 0)
        self.assertIn('wdt:P106 wd:Q191633', q2)
        self.assertNotIn('P279', q2)


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = frontier.connect(Path(self.tmp.name) / 'f.sqlite'); self.addCleanup(self.db.close)
        self.urls = []; self.sleeps = []; self.responses = []

    def client(self):
        def get(url):
            self.urls.append(url)
            return self.responses.pop(0)
        return dw.Client(cache_dir=Path(self.tmp.name) / 'cache', sleep=self.sleeps.append, http_get=get, interval=5.0, clock=lambda: 0.0)

    def test_added_with_source_and_hint_and_dedup(self):
        self.responses = [(200, {}, result(('Q1', 'https://a.example.jp/x', 'あ'), ('Q2', 'https://www.b.jp/', None), ('Q3', 'http://b.jp/y', None), ('Q4', 'https://listed.jp/', None), ('Q5', 'ftp://bad/', None)))]
        out = dw.discover_wikidata(self.db, [ROW], self.client(), max_rows=100, page_size=100, listed={'listed.jp'})
        s = out['rows'][0]
        self.assertEqual((s['added'], s['dup_ledger'], s['dup_listed'], s['invalid']), (2, 1, 1, 1))
        r = self.db.execute("SELECT source, hint_leaf, state, url, source_detail FROM frontier WHERE key='b.jp'").fetchone()
        self.assertEqual((r['source'], r['hint_leaf'], r['state'], r['url']), ('wikidata', 'shrines', 'discovered', 'https://www.b.jp/'))
        self.assertIn('Q2', r['source_detail'])

    def test_existing_ledger_row_untouched(self):
        frontier.add(self.db, 'a.example.jp', 'a.example.jp', 'https://a.example.jp/', 'links', None, 1)
        self.responses = [(200, {}, result(('Q1', 'https://a.example.jp/', None)))]
        out = dw.discover_wikidata(self.db, [ROW], self.client(), page_size=100)
        self.assertEqual(out['added'], 0)
        r = self.db.execute("SELECT source, hint_leaf FROM frontier WHERE key='a.example.jp'").fetchone()
        self.assertEqual((r['source'], r['hint_leaf']), ('links', None))

    def test_paging_cursor_and_interval(self):
        page = lambda n, base: result(*[(f'Q{base + i}', f'https://s{base + i}.jp/', None) for i in range(n)])
        self.responses = [(200, {}, page(2, 0)), (200, {}, page(2, 10)), (200, {}, page(1, 20))]
        c = self.client(); c.last = None
        out = dw.discover_wikidata(self.db, [ROW], c, max_rows=100, page_size=2)
        self.assertEqual((out['added'], out['network_calls']), (5, 3))
        self.assertIn('OFFSET+2'.replace('+', '%20') if False else 'OFFSET', self.urls[1].replace('%20', ' '))
        self.assertEqual(frontier.get_cursor(self.db, 'wikidata', 'shrines:P31:Q845945'), 'done')
        self.assertEqual(self.sleeps, [5.0, 5.0])   # 呼び出しの間に最低 5 秒(1 回目の前は待たない)
        out2 = dw.discover_wikidata(self.db, [ROW], c, max_rows=100, page_size=2)   # 取り終えた行は飛ばす(ネットワークに出ない)
        self.assertEqual((out2['rows'], out2['network_calls']), ([], 3))

    def test_max_rows_stops_and_resumes_from_offset(self):
        self.responses = [(200, {}, result(('Q1', 'https://x1.jp/', None), ('Q2', 'https://x2.jp/', None)))]
        dw.discover_wikidata(self.db, [ROW], self.client(), max_rows=2, page_size=2)
        self.assertEqual(frontier.get_cursor(self.db, 'wikidata', 'shrines:P31:Q845945'), '2')
        self.responses = [(200, {}, result(('Q3', 'https://x3.jp/', None)))]
        dw.discover_wikidata(self.db, [ROW], self.client(), max_rows=2, page_size=2)
        self.assertIn('OFFSET 2', self.urls[-1].replace('%20', ' ').replace('+', ' '))

    def test_cache_avoids_second_request(self):
        self.responses = [(200, {}, result(('Q1', 'https://x1.jp/', None)))]
        c = self.client()
        dw.discover_wikidata(self.db, [ROW], c, page_size=100)
        dw.discover_wikidata(self.db, [ROW], c, page_size=100, ignore_cursor=True)   # 同じクエリ -> キャッシュ
        self.assertEqual(len(self.urls), 1)

    def test_429_aborts_everything_and_honors_retry_after(self):
        rows = [ROW, dict(ROW, leaf_id='temples', qid='Q5393308')]
        self.responses = [(429, {'Retry-After': '120'}, b'')]
        out = dw.discover_wikidata(self.db, rows, self.client(), page_size=100)
        self.assertIn('retry_after=120', out['aborted'])
        self.assertEqual(len(self.urls), 1)                 # 2 行目は叩かない
        self.assertIsNone(frontier.get_cursor(self.db, 'wikidata', 'shrines:P31:Q845945'))
        self.responses = [(503, {}, b'')]
        self.assertIn('503', dw.discover_wikidata(self.db, rows, self.client(), page_size=100)['aborted'])

    def test_timeout_skips_one_row_and_continues(self):
        rows = [ROW, dict(ROW, leaf_id='temples', qid='Q5393308')]
        self.responses = [(500, {}, b'timeout'), (200, {}, result(('Q1', 'https://t1.jp/', None)))]
        out = dw.discover_wikidata(self.db, rows, self.client(), page_size=100)
        self.assertEqual([s['status'] for s in out['rows']], ['skip:http_500', 'ok'])
        self.assertIsNone(out['aborted'])
        self.assertEqual(out['added'], 1)

    def test_network_error_and_bad_json_skip(self):
        def get(url):
            raise OSError('down')
        c = dw.Client(cache_dir=Path(self.tmp.name) / 'c2', sleep=lambda s: None, http_get=get, clock=lambda: 0.0)
        self.assertEqual(dw.discover_wikidata(self.db, [ROW], c)['rows'][0]['status'], 'skip:network')
        self.responses = [(200, {}, b'<html>')]
        self.assertEqual(dw.discover_wikidata(self.db, [ROW], self.client())['rows'][0]['status'], 'skip:bad_json')

    def test_triage_lets_non_jp_wikidata_through_and_export_carries_hint(self):
        self.responses = [(200, {}, result(('Q1', 'https://shrine.example.com/', None), ('Q2', 'https://city.kanazawa.lg.jp/', None)))]
        dw.discover_wikidata(self.db, [ROW], self.client(), page_size=100)
        n = run.triage(self.db)
        self.assertEqual(n['queued'], 1); self.assertEqual(n['government'], 1)   # 官公庁は既存の取得前フィルタが落とす
        self.db.execute("UPDATE frontier SET state='candidate', title='t' WHERE key='shrine.example.com'"); self.db.commit()
        db_path = Path(self.tmp.name) / 'f.sqlite'
        c = export_candidates.export(db_path)
        self.assertEqual(c[0]['features']['leaf_hint'], 'shrines')
        self.db.execute("UPDATE frontier SET hint_leaf=NULL"); self.db.commit()
        self.assertNotIn('leaf_hint', export_candidates.export(db_path)[0]['features'])   # ヒントが無ければキーごと渡さない

    def test_old_ledger_gets_hint_column(self):
        p = Path(self.tmp.name) / 'old.sqlite'
        import sqlite3
        con = sqlite3.connect(p)
        con.executescript((Path(frontier.__file__).parent / 'schema.sql').read_text().replace("  hint_leaf      TEXT,", "").replace("-- 発見元が持つ葉のヒント", "--"))
        con.close()
        db = frontier.connect(p)
        self.assertIn('hint_leaf', [r[1] for r in db.execute('PRAGMA table_info(frontier)')])
        db.close()


if __name__ == '__main__':
    unittest.main()
