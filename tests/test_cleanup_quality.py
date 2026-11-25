"""scripts/cleanup_quality.py (#273 part 2: names / descriptions). Fake D1 = in-memory SQLite built from the repo's REAL migrations; fake LLM.
No network, no production D1, no files."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import cleanup_quality as cq
import discover_online as do
import enrich_online as eo
from enrich import judge_stage as js
from enrich import quality
from test_enrich_publish_stage import FakeD1

LONG_TITLE = '家具の工房ガイド | 手作り家具の通販 | 山田屋の店舗案内 | ご相談ください'     # three clauses, no brand / corporate-form sign: not decidable by rule


def add_site(d1, sid, name, desc='小さな工房の紹介サイト。', url=None, title=None, text_head='', **kw):
    url = url or f'https://{sid}.example.jp/'
    d1.db.execute('INSERT INTO sites (id,url,name,description_ja,description_en) VALUES (?,?,?,?,?)', (sid, url, name, desc, 'A small site.'))
    for k, v in kw.items():
        d1.db.execute(f'UPDATE sites SET {k}=? WHERE id=?', (v, sid))
    pk = d1.one('SELECT progress_key FROM sites WHERE id=?', (sid,))[0]
    if title is not None:
        d1.db.execute('INSERT INTO url_candidates (identity_key,url,title,text_head,host) VALUES (?,?,?,?,?)', (pk, url, title, text_head, sid))
    return pk


class FakeLLM:
    """kind by system prompt: names -> script[host] (else null); rewrite -> '<text without 。>です。' style conversion via script or a default."""
    def __init__(self, names=None, rewrite=None):
        self.calls, self.names, self.rewrite = [], names or {}, rewrite or {}

    def __call__(self, params):
        sysm, text = params['system'][0]['text'], params['messages'][0]['content']
        self.calls.append((sysm[:20], text))
        usage = {'input_tokens': 800, 'output_tokens': 60}
        if 'site_name' in sysm:
            host = re.search(r'host: (\S+)', text).group(1)
            out = {'site_name': self.names.get(host)}
        else:
            items = json.loads(re.search(r'<source>\n(.*)\n</source>', text, re.S).group(1))
            out = {'r': [{'i': it['i'], 't': self.rewrite.get(it['t'], it['t'])} for it in items]}
        return {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}], 'stop_reason': 'end_turn', 'usage': usage}


class Base(unittest.TestCase):
    def setUp(self):
        self.d1, self.out = FakeD1(), []
        for t in ('site_categories', 'site_tags', 'sites'):       # the fake D1 starts with the repo's dev seed sites: only the test's own rows here
            self.d1.db.execute(f'DELETE FROM {t}')

    def go(self, kind, **kw):
        kw.setdefault('apply', True)
        kw.setdefault('llm', FakeLLM())
        code, st = cq.run(self.d1, kind, out=self.out.append, **kw)
        return code, st

    def text(self):
        return '\n'.join(map(str, self.out))

    def name_of(self, sid):
        return self.d1.one('SELECT name FROM sites WHERE id=?', (sid,))[0]

    def snapshot(self):
        return {t: self.d1.db.execute(f'SELECT * FROM {t} ORDER BY 1,2').fetchall() for t in ('sites', 'url_candidates', 'llm_ledger', 'discovery_cursors')}


class Names(Base):
    def seed(self):
        add_site(self.d1, 'ok', '山田屋')                                            # fine
        add_site(self.d1, 'longok', '一般社団法人日本のどこかにあるとても長い名前の協会の公式ホームページではない団体の名称', )   # one clause <= 80 (kept)
        add_site(self.d1, 'rule', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | 公式サイト', url='https://yamada.example.jp/')
        add_site(self.d1, 'llm1', LONG_TITLE, title=LONG_TITLE, url='https://llm-one.example.jp/', text_head='山田工房へようこそ。手作り家具の店です。')
        add_site(self.d1, 'human', LONG_TITLE, title=LONG_TITLE, url='https://nothing.example.jp/')

    def test_dry_run_writes_nothing_and_no_llm(self):
        self.seed()
        before, llm = self.snapshot(), FakeLLM()
        code, st = self.go('names', apply=False, llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(llm.calls, [])
        self.assertEqual(self.d1.writes, 0)
        self.assertEqual(st['selected'], 3)
        self.assertGreaterEqual(st['would_llm'], 2)
        self.assertGreater(st['est_cost_usd_max'], 0)
        self.assertIn('before -> after', self.text())

    def test_apply_updates_reads_back_and_is_idempotent(self):
        self.seed()
        llm = FakeLLM(names={'llm-one.example.jp': '山田屋'})
        code, st = self.go('names', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.name_of('ok'), '山田屋')
        self.assertEqual(self.name_of('longok'), '一般社団法人日本のどこかにあるとても長い名前の協会の公式ホームページではない団体の名称')
        self.assertNotIn('|', self.name_of('rule'))
        self.assertEqual(self.name_of('llm1'), '山田屋')
        self.assertEqual(self.name_of('human'), LONG_TITLE)            # not decidable -> unchanged
        self.assertEqual((st['updated'], st['needs_human'], st['verify_mismatch']), (2, 1, 0))
        self.assertEqual(st['needs_human_ids'], ['human'])
        self.assertIn("needs_human ids: ['human']", self.text())
        # second run: nothing left to fix except the human row (re-tried once after the table end -> cursor wrapped)
        calls = len(llm.calls)
        code, st2 = self.go('names', llm=llm)
        self.assertEqual((st2['updated'], st2['by_rule'], st2['by_llm']), (0, 0, 0))
        self.assertEqual(self.name_of('llm1'), '山田屋')
        self.assertLessEqual(len(llm.calls) - calls, 1)
        self.assertEqual(self.d1.max_params <= 100, True)

    def test_original_title_kept(self):
        self.seed()
        self.go('names', llm=FakeLLM(names={'llm-one.example.jp': '山田屋'}))
        self.assertEqual(self.d1.one("SELECT title FROM url_candidates WHERE host='llm1'")[0], LONG_TITLE)

    def test_llm_answer_must_be_verbatim_and_short(self):
        self.seed()
        llm = FakeLLM(names={'llm-one.example.jp': '山田製作所'})        # invented name: not in the source
        code, st = self.go('names', llm=llm)
        self.assertEqual(self.name_of('llm1'), LONG_TITLE)
        self.assertEqual(st['needs_human'], 2)

    def test_search_text_follows_name_and_fts_consistent(self):
        self.seed()
        db = self.d1.db
        db.execute("UPDATE sites SET search_text=lower(name) WHERE 1")
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('rebuild')")
        db.execute("UPDATE fts_state SET ready=1 WHERE name='sites_fts'")
        self.go('names', llm=FakeLLM(names={'llm-one.example.jp': '山田屋'}))
        st_text = self.d1.one("SELECT search_text FROM sites WHERE id='llm1'")[0]
        self.assertIn('山田屋', st_text)
        self.assertNotIn('ご相談ください', st_text)
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('integrity-check')")        # raises when the index disagrees with the content
        hit = db.execute("SELECT s.id FROM sites_fts f JOIN sites s ON s.rowid=f.rowid WHERE sites_fts MATCH '\"山田屋\"'").fetchall()
        self.assertIn('llm1', [r[0] for r in hit])

    def test_adult_and_name_ja_untouched(self):
        add_site(self.d1, 'ad', LONG_TITLE, title=LONG_TITLE, adult_content=1, name_ja='別名', country_code='JP')
        self.go('names', llm=FakeLLM(names={'ad.example.jp': '山田屋'}))
        row = self.d1.one("SELECT name, name_ja, adult_content, country_code FROM sites WHERE id='ad'")
        self.assertEqual(tuple(row), ('山田屋', '別名', 1, 'JP'))

    def test_limit_and_cursor_resume(self):
        for n in range(5):
            add_site(self.d1, f's{n}', LONG_TITLE, title=LONG_TITLE, url=f'https://h{n}.example.jp/')
        llm = FakeLLM(names={f'h{n}.example.jp': '山田屋' for n in range(5)})
        code, st = self.go('names', limit=2, llm=llm)
        self.assertEqual(st['updated'], 2)
        self.assertEqual(cq.get_cursor(self.d1, 'names'), 's1')
        self.go('names', limit=2, llm=llm)
        self.go('names', limit=2, llm=llm)
        self.assertEqual([self.name_of(f's{n}') for n in range(5)], ['山田屋'] * 5)
        self.assertEqual(cq.get_cursor(self.d1, 'names'), '')           # the table end wrapped the cursor

    def test_budget_stop(self):
        self.seed()
        llm = FakeLLM(names={'llm-one.example.jp': '山田屋'})
        code, st = self.go('names', llm=llm, budget_month=0.000001)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(llm.calls, [])
        self.assertIn('monthly cap', self.text())
        self.assertEqual(self.name_of('llm1'), LONG_TITLE)
        self.assertGreaterEqual(st['not_reached'], 1)
        self.assertEqual(self.name_of('rule'), self.name_of('rule'))

    def test_api_refusal_stops_with_exit_1_and_releases(self):
        self.seed()

        def refuse(params):
            raise js.LLMRefused('overloaded')
        code, st = self.go('names', llm=refuse)
        self.assertEqual(code, 1, self.text())
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM llm_ledger WHERE kind='reserve'")[0], 0)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM llm_ledger WHERE kind='release'")[0], 1)

    def test_rate_limit_exit_3(self):
        self.seed()

        def limited(params):
            raise js.LLMRefused('API error 429 rate_limit')
        code, _ = self.go('names', llm=limited)
        self.assertEqual(code, 3)

    def test_ledger_settle_failure_exit_5(self):
        self.seed()
        orig = self.d1.query

        def q(sql, params=()):
            if sql.startswith("UPDATE llm_ledger SET kind='call'"):
                raise RuntimeError('d1 down')
            return orig(sql, params)
        self.d1.query = q
        code, _ = self.go('names', llm=FakeLLM(names={'llm-one.example.jp': '山田屋'}))
        self.assertEqual(code, 5)

    def test_verify_mismatch_exit_1(self):
        self.seed()
        orig = self.d1.batch

        def b(stmts):
            r = orig(stmts)
            self.d1.db.execute("UPDATE sites SET name='???' WHERE id='rule'")      # something else rewrote it
            return r
        self.d1.batch = b
        code, st = self.go('names', llm=FakeLLM(names={'llm-one.example.jp': '山田屋'}))
        self.assertEqual(code, 1)
        self.assertEqual(st['verify_mismatch_ids'], ['rule'])

    def test_row_changed_meanwhile_is_skipped(self):
        add_site(self.d1, 'r', LONG_TITLE, title=LONG_TITLE, url='https://r.example.jp/')
        orig = self.d1.batch
        self.d1.batch = lambda s: (self.d1.db.execute("UPDATE sites SET name='他の人が直した名前' WHERE id='r'"), orig(s))[1]
        code, st = self.go('names', llm=FakeLLM(names={'r.example.jp': '山田屋'}))
        self.assertEqual(self.name_of('r'), '他の人が直した名前')
        self.assertEqual((code, st['skipped_changed'], st['verify_mismatch']), (0, 1, 0))


class Leasing(Base):
    def test_lease_held_by_other_exits_2_and_writes_nothing(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        other = do.Lease(self.d1, key='cleanup', owner='someone-else')
        self.assertTrue(other.acquire())
        before = self.snapshot()
        code, st = self.go('names')
        self.assertEqual(code, 2)
        self.assertEqual(self.snapshot(), before)

    def test_lease_released_and_key(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        self.go('names')
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='lease'")[0], 0)

    def test_lease_lost_exit_4(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)

        class Lost(do.Lease):
            def renew(self):
                raise do.LeaseLost('gone')
        lease = Lost(self.d1, key='cleanup')
        ticks = iter(range(0, 10000, 500))
        code, _ = self.go('names', lease=lease, clock=lambda: next(ticks))
        self.assertEqual(code, 4)

    def test_bookmark_printed_first_and_failure_is_warning(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        orig = eo.time_travel_bookmark
        eo.time_travel_bookmark = lambda c: 'bm-xyz'
        try:
            self.go('names', llm=FakeLLM(names={'a.example.jp': '山田屋'}))
        finally:
            eo.time_travel_bookmark = orig
        self.assertIn('bm-xyz', self.text())
        self.out.clear()
        add_site(self.d1, 'b', LONG_TITLE, title=LONG_TITLE)
        eo.time_travel_bookmark = lambda c: (_ for _ in ()).throw(RuntimeError('no token'))
        try:
            code, _ = self.go('names', llm=FakeLLM(names={'b.example.jp': '山田屋'}))
        finally:
            eo.time_travel_bookmark = orig
        self.assertEqual(code, 0)
        self.assertIn('WARNING', self.text())


PLAIN_RULE = '手作りの家具を作っている。'
PLAIN_HARD = '独自の理論に基づき、作品を紹介している。ご注文は随時受け付ける。'
PLAIN_LLM = '山田氏が2020年に始めた活動を記録する。「ひだまり」の様子を載せた。'


class Descriptions(Base):
    def seed(self):
        add_site(self.d1, 'good', '山田屋', desc='家具工房の紹介サイト。')
        add_site(self.d1, 'rule', '山田屋', desc=PLAIN_RULE)
        add_site(self.d1, 'llm', '山田屋', desc=PLAIN_LLM)
        add_site(self.d1, 'bad', '山田屋', desc='作品を載せた。')

    def desc(self, sid):
        return self.d1.one('SELECT description_ja FROM sites WHERE id=?', (sid,))[0]

    def test_dry_run(self):
        self.seed()
        before, llm = self.snapshot(), FakeLLM()
        code, st = self.go('descriptions', apply=False, llm=llm)
        self.assertEqual((code, llm.calls, self.d1.writes), (0, [], 0))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(st['selected'], 3)
        self.assertIn('(LLM)', self.text())

    def test_apply(self):
        self.seed()
        self.assertEqual(quality.to_polite(PLAIN_RULE), '手作りの家具を作っています。')
        llm = FakeLLM(rewrite={PLAIN_LLM: '山田氏が2020年に始めた活動を記録しています。「ひだまり」の様子を載せました。',
                               '作品を載せた。': '作品を載せた内容に新しい発表が3件あります。'})
        code, st = self.go('descriptions', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.desc('good'), '家具工房の紹介サイト。')
        self.assertEqual(self.desc('rule'), '手作りの家具を作っています。')
        self.assertEqual(self.desc('llm'), '山田氏が2020年に始めた活動を記録しています。「ひだまり」の様子を載せました。')
        self.assertEqual(self.desc('bad'), '作品を載せた。')                 # a new number -> rejected
        self.assertEqual((st['by_rule'], st['by_llm'], st['needs_human'], st['llm_calls']), (1, 1, 1, 1))     # ONE call for the batch
        self.assertEqual(st['needs_human_ids'], ['bad'])
        self.assertIn('ひだまり', self.d1.one("SELECT search_text FROM sites WHERE id='llm'")[0])
        code, st2 = self.go('descriptions', llm=llm)
        self.assertEqual((st2['updated'], st2['by_rule'], st2['by_llm']), (0, 0, 0))

    def test_english_untouched(self):
        self.seed()
        self.go('descriptions', llm=FakeLLM())
        self.assertEqual(self.d1.one("SELECT DISTINCT description_en FROM sites")[0], 'A small site.')

    def test_batches_of_ten(self):
        for n in range(23):
            add_site(self.d1, f'p{n:02d}', '山田屋', desc=f'毎月{n}回の勉強会を行う。')
        llm = FakeLLM()
        self.go('descriptions', llm=llm, limit=100)
        self.assertEqual(len(llm.calls), 3)

    def test_check_rewrite(self):
        ok = cq.check_rewrite
        self.assertEqual(ok('作品を載せた。', '作品を載せました。'), (True, ''))
        self.assertEqual(ok('作品を載せた。', '作品を載せた。')[0], False)
        self.assertEqual(ok('Linuxの話を書いた。', 'Windowsの話を書きました。'), (False, 'latin'))
        self.assertEqual(ok('2020年に始めた。', '2021年に始めました。'), (False, 'numbers'))
        self.assertEqual(ok('「ひだまり」を載せた。', 'ひなたを載せました。'), (False, 'quoted'))
        self.assertEqual(ok('作品を載せた。', '作品を載せましたが、それ以外にも様々な活動の記録を毎週詳しく丁寧に載せています。'), (False, 'length'))
        self.assertFalse(ok('作品を載せた。', None)[0])


class Misc(Base):
    def test_limit_bounds_and_cheapest_model(self):
        with self.assertRaises(ValueError):
            cq.run(self.d1, 'names', limit=1001)
        self.assertEqual(cq.CHEAP_MODEL, js.judge.MODELS['haiku'])

    def test_main_parses_and_default_is_dry_run(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        before = self.snapshot()
        self.assertEqual(cq.main(['names'], client=self.d1), 0)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(SystemExit):
            cq.main(['names', '--limit', '5000'], client=self.d1)

    def test_no_files_created(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.go('names', llm=FakeLLM(names={'a.example.jp': '山田屋'}))
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(cwd)


if __name__ == '__main__':
    unittest.main()
