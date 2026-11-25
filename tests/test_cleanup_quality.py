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

LONG_TITLE = 'あ' * 40 + '(山田屋)' + 'い' * 45     # 90 chars, no clause punctuation / space to cut at: the rule gives no name (needs_human); the LLM fallback may


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
        self._bm = eo.time_travel_bookmark
        eo.time_travel_bookmark = lambda c: 'bm-test'
        self.addCleanup(lambda: setattr(eo, 'time_travel_bookmark', self._bm))
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


def dump(d1, sid):
    return tuple(d1.one('SELECT name, name_ja, name_en FROM sites WHERE id=?', (sid,)))


class Names(Base):
    """names: the plain rule (quality.derive_name). Selected rows = the stored name still fails the length / quality check and the rule would change it."""
    def seed(self):
        add_site(self.d1, 'ok', '山田屋')                                            # fine
        add_site(self.d1, 'longok', '一般社団法人日本のどこかにあるとても長い名前の協会の公式ホームページではない団体の名称', )   # one clause <= 80 (kept)
        add_site(self.d1, 'rule', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | 公式サイト', url='https://yamada.example.jp/')
        add_site(self.d1, 'pair', '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', title='愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', url='https://aitech.example.jp/')
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
        self.assertEqual((st['selected'], st['would_llm']), (4, 0))          # rule, pair, llm1, human: no LLM unless --llm-fallback
        self.assertIn('before -> after', self.text())
        self.assertIn("ja='愛知工業大学'", self.text())

    def test_dry_run_with_fallback_estimates_the_llm_cost_only_for_undecided_rows(self):
        self.seed()
        _c, st = self.go('names', apply=False, llm_fallback=True)
        self.assertEqual(st['would_llm'], 2)
        self.assertGreater(st['est_cost_usd_max'], 0)

    def test_apply_updates_reads_back_and_is_idempotent_without_any_llm(self):
        self.seed()
        llm = FakeLLM()
        code, st = self.go('names', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(dump(self.d1, 'ok'), ('山田屋', None, None))
        self.assertEqual(self.name_of('longok'), '一般社団法人日本のどこかにあるとても長い名前の協会の公式ホームページではない団体の名称')
        self.assertEqual(dump(self.d1, 'rule'), ('山田屋', None, None))
        self.assertEqual(dump(self.d1, 'pair'), ('愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', '愛知工業大学', 'AICHI INSTITUTE OF TECHNOLOGY'))
        self.assertEqual(self.name_of('llm1'), LONG_TITLE)                  # no answer by the rule: unchanged (LLM is off by default)
        self.assertEqual((st['updated'], st['needs_human'], st['verify_mismatch']), (2, 2, 0))
        self.assertEqual(sorted(st['needs_human_ids']), ['human', 'llm1'])
        self.assertEqual(llm.calls, [])
        code, st2 = self.go('names', llm=llm, restart=True)
        self.assertEqual((st2['updated'], st2['by_rule'], st2['needs_human']), (0, 0, 0))     # the listed rows are skipped now
        self.assertLessEqual(self.d1.max_params, 100)

    def test_existing_name_ja_en_are_never_overwritten_and_one_language_is_not_copied(self):
        t = '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY'
        add_site(self.d1, 'has_ja', t, title=t, url='https://a.example.jp/', name_ja='手書きの名前')
        add_site(self.d1, 'has_both', t, title=t, url='https://b.example.jp/', name_ja='甲', name_en='B')
        t2 = 'x' * 30 + ' | ' + 'y' * 30
        add_site(self.d1, 'one_lang', t2, title='Yamada Shop | 公式サイト | ホーム', url='https://c.example.jp/')
        self.go('names')
        self.assertEqual(dump(self.d1, 'has_ja'), (t, '手書きの名前', 'AICHI INSTITUTE OF TECHNOLOGY'))
        self.assertEqual(dump(self.d1, 'has_both'), (t, '甲', 'B'))                     # the name itself is already right: only nothing to do
        self.assertEqual(dump(self.d1, 'one_lang'), ('Yamada Shop', None, None))

    def test_the_update_statement_itself_keeps_a_value_written_meanwhile(self):
        t = '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY'
        add_site(self.d1, 'race', t, title=t, url='https://r.example.jp/')
        orig = self.d1.batch
        self.d1.batch = lambda st: (self.d1.db.execute("UPDATE sites SET name_ja='途中で入った' WHERE id='race'"), orig(st))[1]
        self.go('names')
        self.assertEqual(dump(self.d1, 'race')[1], '途中で入った')

    def test_original_title_kept(self):
        add_site(self.d1, 'nt', 'x' * 30 + ' | ' + 'y' * 30, url='https://nt.example.jp/')          # no url_candidates row, name = source: Home -> nothing -> human
        add_site(self.d1, 'wt', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | ホーム', url='https://wt.example.jp/')
        self.go('names')
        self.assertEqual(self.d1.one("SELECT title FROM url_candidates WHERE host='wt'")[0], '山田屋 | ホーム')    # an existing title is never overwritten
        self.assertEqual(self.name_of('wt'), '山田屋')

    def test_old_name_goes_to_url_candidates_when_there_is_no_title(self):
        old = '山田屋 | 公式サイト | ホーム | トップページ'
        add_site(self.d1, 'nt', old + 'あ' * 90, url='https://nt.example.jp/')              # > 80, no title: derived from the name
        self.go('names')
        pk = self.d1.one("SELECT progress_key FROM sites WHERE id='nt'")[0]
        self.assertEqual(self.name_of('nt'), '山田屋')
        self.assertEqual(self.d1.one('SELECT title FROM url_candidates WHERE identity_key=?', (pk,))[0], old + 'あ' * 90)
        self.assertIn(repr(old + 'あ' * 90), self.text())                                  # every (id, old, new) is printed on apply

    def test_search_text_and_fts_follow_name_ja_en(self):
        self.seed()
        db = self.d1.db
        db.execute("UPDATE sites SET search_text=lower(name) WHERE 1")
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('rebuild')")
        db.execute("UPDATE fts_state SET ready=1 WHERE name='sites_fts'")
        self.go('names')
        st_text = self.d1.one("SELECT search_text FROM sites WHERE id='pair'")[0]
        self.assertIn('愛知工業大学', st_text)
        self.assertIn('aichi institute of technology', st_text)
        self.assertIn('山田屋', self.d1.one("SELECT search_text FROM sites WHERE id='rule'")[0])
        self.assertNotIn('yyyy', self.d1.one("SELECT search_text FROM sites WHERE id='rule'")[0])
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('integrity-check')")        # raises when the index disagrees with the content
        hit = db.execute("SELECT s.id FROM sites_fts f JOIN sites s ON s.rowid=f.rowid WHERE sites_fts MATCH '\"山田屋\"'").fetchall()
        self.assertIn('rule', [r[0] for r in hit])

    def test_name_en_only_change_retriggers_search_text(self):
        t = '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY'
        add_site(self.d1, 'p', t, title=t, url='https://p.example.jp/', name_en='ZZZ Tech')
        self.d1.db.execute("UPDATE sites SET search_text=lower(name || ' ' || name_en) WHERE id='p'")
        self.go('names')
        self.assertIn('愛知工業大学', dump(self.d1, 'p')[1])                                   # only name_ja was empty: it is filled, name_en kept
        self.assertEqual(dump(self.d1, 'p')[2], 'ZZZ Tech')
        self.assertIn('zzz tech', self.d1.one("SELECT search_text FROM sites WHERE id='p'")[0])

    def test_adult_uses_host_like_publish_and_name_ja_untouched(self):
        add_site(self.d1, 'ad', LONG_TITLE, title=LONG_TITLE, adult_content=1, name_ja='別名', country_code='JP', url='https://ad.example.jp/')
        llm = FakeLLM(names={'ad.example.jp': '山田屋'})
        self.go('names', llm=llm, llm_fallback=True)
        row = self.d1.one("SELECT name, name_ja, adult_content, country_code FROM sites WHERE id='ad'")
        self.assertEqual(tuple(row), ('ad.example.jp', '別名', 1, 'JP'))
        self.assertEqual(llm.calls, [])

    def test_needs_human_is_marked_and_skipped_until_retry(self):
        self.seed()
        self.go('names')
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip'")[0], 2)
        code, st = self.go('names', restart=True)
        self.assertEqual((st['skip_listed'], st['needs_human']), (2, 0))
        llm = FakeLLM(names={'nothing.example.jp': '山田屋'})
        code, st = self.go('names', llm=llm, restart=True, retry_skipped=True, llm_fallback=True)
        self.assertEqual((st['updated'], self.name_of('human'), self.name_of('llm1')), (1, '山田屋', LONG_TITLE))
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip'")[0], 1)

    def test_llm_is_called_only_with_the_flag_and_only_for_rows_the_rule_cannot_decide(self):
        self.seed()
        llm = FakeLLM(names={'llm-one.example.jp': '山田屋'})
        self.go('names', llm=llm)
        self.assertEqual(llm.calls, [])
        self.go('names', llm=llm, llm_fallback=True, retry_skipped=True, restart=True)
        self.assertEqual(len(llm.calls), 2)                              # llm1 and human only; 'rule' and 'pair' never
        self.assertEqual(self.name_of('llm1'), '山田屋')
        self.assertEqual(self.name_of('human'), LONG_TITLE)             # no answer / not a whole piece of the source: unchanged

    def test_llm_answer_must_be_verbatim_and_short(self):
        add_site(self.d1, 'llm1', LONG_TITLE, title=LONG_TITLE, url='https://llm-one.example.jp/')
        llm = FakeLLM(names={'llm-one.example.jp': '山田製作所'})        # invented name: not in the source
        code, st = self.go('names', llm=llm, llm_fallback=True)
        self.assertEqual(self.name_of('llm1'), LONG_TITLE)
        self.assertEqual(st['needs_human'], 1)

    def test_stored_site_name_used_before_llm_in_fallback(self):
        add_site(self.d1, 'st', LONG_TITLE, title=LONG_TITLE, url='https://st.example.jp/')
        self.d1.db.execute("UPDATE url_candidates SET site_name='山田屋' WHERE host='st'")
        llm = FakeLLM()
        self.go('names', llm=llm, llm_fallback=True)
        self.assertEqual((self.name_of('st'), llm.calls), ('山田屋', []))
        add_site(self.d1, 'st2', LONG_TITLE, title=LONG_TITLE, url='https://st2.example.jp/')
        self.d1.db.execute("UPDATE url_candidates SET site_name='山田屋' WHERE host='st2'")
        self.go('names', llm=llm)                                          # flag off: the stored name is not used either
        self.assertEqual(self.name_of('st2'), LONG_TITLE)

    def test_guard_uses_stored_value_not_collapsed(self):
        odd = 'Ｙ工房  ｜ ' + 'あ' * 40 + ' ｜ ' + 'い' * 40                  # a double space: collapse() would not equal the stored text
        add_site(self.d1, 'w', odd, title='Ｙ工房 | 公式サイト | ホーム', url='https://w.example.jp/')
        code, st = self.go('names')
        self.assertEqual((st['updated'], st['skipped_changed']), (1, 0), self.text())
        self.assertEqual(self.name_of('w'), 'Ｙ工房')

    def test_written_by_chunks_so_a_stop_keeps_paid_results(self):
        for n in range(3):
            add_site(self.d1, f'k{n}', LONG_TITLE, title=LONG_TITLE, url=f'https://k{n}.example.jp/')
        inner, seen = FakeLLM(names={f'k{n}.example.jp': '山田屋' for n in range(3)}), []

        def second_refused(params):
            if seen:
                raise js.LLMRefused('overloaded')
            seen.append(1)
            return inner(params)
        code, st = self.go('names', llm=second_refused, llm_fallback=True)
        self.assertEqual(code, 1)
        self.assertEqual([self.name_of(f'k{n}') for n in range(3)], ['山田屋', LONG_TITLE, LONG_TITLE])
        self.assertEqual(cq.get_cursor(self.d1, 'names'), '')

    def test_predicate_and_call_clauses_are_not_llm_names(self):
        for bad in ('ご相談ください', '手作り家具の店です', '一緒に作りましょう', '家具を作る', '気軽に相談できる', '皆で作ろう', 'お待ちしています', '家具を作っています。'):
            self.assertTrue(cq.predicate_like(bad), bad)
            self.assertIsNone(cq.llm_name_valid(bad, [bad]), bad)
        for good in ('山田屋の店', '手作り家具の店', 'American Express', '山田屋', 'おやつ工房', 'ほたる'):
            self.assertFalse(cq.predicate_like(good), good)

    def test_llm_name_must_be_a_whole_piece_of_one_source(self):
        v = cq.llm_name_valid
        self.assertIsNone(v('American Expr', ['American Express']))
        self.assertIsNone(v('rican Express', ['American Express']))
        self.assertIsNone(v('山田製', ['山田製作所']))
        self.assertIsNone(v('作所｜精密', ['山田製作所｜精密']))
        self.assertIsNone(v('山田屋 精密', ['山田屋', '精密']))                    # joined sources do not count
        self.assertEqual(v('American Express', ['American Express Japan']), 'American Express')
        self.assertEqual(v('山田製作所', ['山田製作所｜精密工業のご案内']), '山田製作所')

    def test_llm_answer_cannot_be_a_service_or_error_page(self):
        self.assertIsNone(cq.llm_name_valid('Peatix', ['山田 | Peatix'], 'a.peatix.com'))
        self.assertIsNone(cq.llm_name_valid('404 Not Found', ['404 Not Found | 山田屋']))
        self.assertIsNone(cq.llm_name_valid('Official Dealer Site', ['ジープ厚木｜Official Dealer Site']))

    def test_limit_and_cursor_resume(self):
        for n in range(5):
            add_site(self.d1, f's{n}', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | ホーム', url=f'https://h{n}.example.jp/')
        code, st = self.go('names', limit=2)
        self.assertEqual(st['updated'], 2)
        self.assertEqual(cq.get_cursor(self.d1, 'names'), 's1')
        self.go('names', limit=2)
        self.go('names', limit=2)
        self.assertEqual([self.name_of(f's{n}') for n in range(5)], ['山田屋'] * 5)
        self.assertEqual(cq.get_cursor(self.d1, 'names'), '')           # the table end wrapped the cursor

    def test_budget_stop(self):
        self.seed()
        llm = FakeLLM(names={'llm-one.example.jp': '山田屋'})
        code, st = self.go('names', llm=llm, budget_month=0.000001, llm_fallback=True)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(llm.calls, [])
        self.assertIn('monthly cap', self.text())
        self.assertEqual(self.name_of('llm1'), LONG_TITLE)
        self.assertGreaterEqual(st['not_reached'], 1)

    def test_api_refusal_stops_with_exit_1_and_releases(self):
        self.seed()

        def refuse(params):
            raise js.LLMRefused('overloaded')
        code, st = self.go('names', llm=refuse, llm_fallback=True)
        self.assertEqual(code, 1, self.text())
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM llm_ledger WHERE kind='reserve'")[0], 0)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM llm_ledger WHERE kind='release'")[0], 1)

    def test_rate_limit_exit_3(self):
        self.seed()

        def limited(params):
            raise js.LLMRefused('API error 429 rate_limit')
        code, _ = self.go('names', llm=limited, llm_fallback=True)
        self.assertEqual(code, 3)

    def test_ledger_settle_failure_exit_5(self):
        self.seed()
        orig = self.d1.query

        def q(sql, params=()):
            if sql.startswith("UPDATE llm_ledger SET kind='call'"):
                raise RuntimeError('d1 down')
            return orig(sql, params)
        self.d1.query = q
        code, _ = self.go('names', llm=FakeLLM(names={'llm-one.example.jp': '山田屋'}), llm_fallback=True)
        self.assertEqual(code, 5)

    def test_verify_mismatch_exit_1(self):
        self.seed()
        orig = self.d1.batch

        def b(stmts):
            r = orig(stmts)
            self.d1.db.execute("UPDATE sites SET name='???' WHERE id='rule'")      # something else rewrote it
            return r
        self.d1.batch = b
        code, st = self.go('names')
        self.assertEqual(code, 1)
        self.assertEqual(st['verify_mismatch_ids'], ['rule'])

    def test_row_changed_meanwhile_is_skipped(self):
        add_site(self.d1, 'r', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | ホーム', url='https://r.example.jp/')
        orig = self.d1.batch
        self.d1.batch = lambda s: (self.d1.db.execute("UPDATE sites SET name='他の人が直した名前' WHERE id='r'"), orig(s))[1]
        code, st = self.go('names')
        self.assertEqual(self.name_of('r'), '他の人が直した名前')
        self.assertEqual((code, st['skipped_changed'], st['verify_mismatch']), (0, 1, 0))


# the owner's real titles: (title as stored, host, the name a previous run left (a piece of the title), expected name, ja, en)
REAL = [
    ('木更津市の不動産会社｜アクアエステート木更津｜千葉県袖ケ浦・木更津・君津・富津の不動産・住宅情報', 'aqua.example.jp', 'アクアエステート木更津',
     '木更津市の不動産会社｜アクアエステート木更津｜千葉県袖ケ浦・木更津・君津・富津の不動産・住宅情報', None, None),
    ('石川県白山市にあるアラヤ接骨院 | 肩こり・腰痛・体の歪み・マッサージ', 'araya.example.jp', 'アラヤ接骨院',
     '石川県白山市にあるアラヤ接骨院 | 肩こり・腰痛・体の歪み・マッサージ', None, None),
    ('TOP│朝倉真弓公式サイト｜ウォーキング講師・文筆家・講演家', 'asakura.example.jp', '朝倉真弓', '朝倉真弓公式サイト｜ウォーキング講師・文筆家・講演家', None, None),
    ('トップページ｜富山県中央植物園ウェブサイト076-466-4187', 'toyama.example.jp', '富山県中央植物園', '富山県中央植物園ウェブサイト', None, None),
    ('ジープ厚木｜Jeep Official Dealer Site', 'jeep.example.jp', 'ジープ厚木', 'ジープ厚木｜Jeep Official Dealer Site', 'ジープ厚木', 'Jeep Official Dealer Site'),
    ('愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', 'ait.example.jp', '愛知工業大学', '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', '愛知工業大学', 'AICHI INSTITUTE OF TECHNOLOGY'),
    ('日本抗加齢医学会 | Japanese Society of Anti-aging Medicine', 'jaam.example.jp', '日本抗加齢医学会', '日本抗加齢医学会 | Japanese Society of Anti-aging Medicine',
     '日本抗加齢医学会', 'Japanese Society of Anti-aging Medicine'),
    ('2023年度 小児AA/MDS治療研究会学術集会 ハイブリッド開催(現地とWeb併用) | Peatix', 'aa.peatix.com', '2023年度 小児AA/MDS治療研究会学術集会',
     '2023年度 小児AA/MDS治療研究会学術集会 ハイブリッド開催(現地とWeb併用)', None, None),
    ('100s [Hyaku-Shiki] Official site – 100s.jp100s [Hyaku-Shiki] Official site - 100', '100s.example.jp', '100s', '100s [Hyaku-Shiki] Official site', None, None),
]


class Recompute(Base):
    """names --recompute: (a) rows still failing the check, (b) rows an earlier run rewrote (the stored name is a piece of the original title, or differs from
    the rule's result while being shorter than the title). Rows already equal to the rule's result are untouched."""
    def seed(self):
        for n, (title, host, old, _new, _ja, _en) in enumerate(REAL):
            add_site(self.d1, f'r{n}', old, title=title, url=f'https://{host}/')
        add_site(self.d1, 'good', '山田屋', title='山田屋', url='https://yamada.example.jp/')             # name == title: not selected
        add_site(self.d1, 'plain', '山田屋', url='https://plain.example.jp/')                              # no title at all: not selected

    def test_dry_run_prints_every_row_with_ja_en_and_writes_nothing(self):
        self.seed()
        before = self.snapshot()
        code, st = self.go('names-recompute', apply=False, limit=1000)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.d1.writes, 0)
        self.assertEqual(st['selected'], len(REAL))
        self.assertEqual(st['would_llm'], 0)
        for _t, _h, old, new, ja, en in REAL:
            self.assertIn(f'- {old}', self.text())
            self.assertIn(f'+ {new}', self.text())
        self.assertIn("ja='ジープ厚木' en='Jeep Official Dealer Site'", self.text())

    def test_apply_gives_the_literal_rule_results_and_is_idempotent(self):
        self.seed()
        code, st = self.go('names-recompute', limit=1000)
        self.assertEqual((code, st['updated'], st['needs_human']), (0, len(REAL), 0), self.text())
        for n, (_t, _h, _old, new, ja, en) in enumerate(REAL):
            self.assertEqual(dump(self.d1, f'r{n}'), (new, ja, en), n)
        self.assertEqual(dump(self.d1, 'good'), ('山田屋', None, None))
        code, st2 = self.go('names-recompute', limit=1000, restart=True)
        self.assertEqual((st2['selected'], st2['updated']), (0, 0))

    def test_a_row_equal_to_the_rule_is_untouched(self):
        t = '愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY'
        add_site(self.d1, 'same', t, title=t, url='https://same.example.jp/', name_ja='愛知工業大学', name_en='AICHI INSTITUTE OF TECHNOLOGY')
        before = self.snapshot()
        code, st = self.go('names-recompute', limit=1000)
        self.assertEqual((st['selected'], st['updated']), (0, 0))
        self.assertEqual(self.snapshot()['sites'], before['sites'])

    def test_a_shorter_name_that_is_not_a_piece_of_the_title_is_also_recomputed(self):
        add_site(self.d1, 'x', 'アラヤ接骨院', title='アラヤ接骨院 | 肩こり・腰痛', url='https://x.example.jp/')
        add_site(self.d1, 'y', '別名のお店', title='アラヤ接骨院 | 肩こり・腰痛', url='https://y.example.jp/')        # not a substring, but shorter and different
        self.go('names-recompute', limit=1000)
        self.assertEqual((self.name_of('x'), self.name_of('y')), ('アラヤ接骨院 | 肩こり・腰痛', 'アラヤ接骨院 | 肩こり・腰痛'))

    def test_the_host_name_is_never_the_name(self):
        add_site(self.d1, 'h', '100s', title='100s.jp | ホーム', url='https://100s.jp/')
        code, st = self.go('names-recompute', limit=1000)
        self.assertEqual((self.name_of('h'), st['needs_human'], st['updated']), ('100s', 1, 0))

    def test_old_rewritten_name_is_kept_nowhere_but_the_original_title_stays(self):
        self.seed()
        self.go('names-recompute', limit=1000)
        self.assertEqual(self.d1.one("SELECT title FROM url_candidates WHERE host='r4'")[0], REAL[4][0])

    def test_recompute_guard_uses_stored_value(self):
        add_site(self.d1, 'g', 'アラヤ接骨院', title='アラヤ接骨院 | 肩こり・腰痛', url='https://g.example.jp/')
        orig = self.d1.batch
        self.d1.batch = lambda s: (self.d1.db.execute("UPDATE sites SET name='人が直した' WHERE id='g'"), orig(s))[1]
        code, st = self.go('names-recompute')
        self.assertEqual((self.name_of('g'), st['skipped_changed']), ('人が直した', 1))

    def test_llm_is_off_unless_asked(self):
        add_site(self.d1, 'u', 'あ' * 90, title='あ' * 40 + '(山田屋)' + 'い' * 45, url='https://u.example.jp/')
        llm = FakeLLM(names={'u.example.jp': '山田屋'})
        code, st = self.go('names-recompute', llm=llm)
        self.assertEqual((llm.calls, st['needs_human']), ([], 1))
        code, st = self.go('names-recompute', llm=llm, restart=True, retry_skipped=True, llm_fallback=True)
        self.assertEqual((len(llm.calls), self.name_of('u')), (1, '山田屋'))

    def test_flags_only_with_names(self):
        for flag in ('--recompute', '--llm-fallback'):
            with self.assertRaises(SystemExit):
                cq.main(['descriptions', flag], client=self.d1)

    def test_main_names_recompute_is_dry_run_by_default(self):
        self.seed()
        before = self.snapshot()
        self.assertEqual(cq.main(['names', '--recompute'], client=self.d1), 0)
        self.assertEqual(self.snapshot(), before)


class Boundary(Base):
    def test_quality_gate_function_still_lets_a_long_formal_name_stand(self):
        self.assertTrue(cq.name_ok('あ' * 70))
        self.assertFalse(cq.name_ok('あ' * 90))
        self.assertFalse(cq.name_needs_fix('山田屋'))

    def test_domain_like(self):
        self.assertTrue(cq.domain_like('100s.jp', '100s.jp'))
        self.assertTrue(cq.domain_like('www.example.com'))
        self.assertFalse(cq.domain_like('Yahoo! JAPAN', 'yahoo.co.jp'))


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
        add_site(self.d1, 'a', 'x' * 30 + ' | ' + 'y' * 30, title='山田屋 | ホーム')

        class Lost(do.Lease):
            def renew(self):
                raise do.LeaseLost('gone')
        lease = Lost(self.d1, key='cleanup')
        ticks = iter(range(0, 10000, 500))
        code, _ = self.go('names', lease=lease, clock=lambda: next(ticks))
        self.assertEqual(code, 4)

    def test_bookmark_printed_and_failure_aborts_unless_allowed(self):
        add_site(self.d1, 'a', LONG_TITLE, title=LONG_TITLE)
        llm = FakeLLM(names={'a.example.jp': '山田屋'})
        self.go('names', llm=llm, llm_fallback=True)
        self.assertIn('bm-test', self.text())
        add_site(self.d1, 'b', LONG_TITLE, title=LONG_TITLE)
        eo.time_travel_bookmark = lambda c: (_ for _ in ()).throw(RuntimeError('no token'))
        llm2 = FakeLLM(names={'b.example.jp': '山田屋'})
        code, _ = self.go('names', llm=llm2, llm_fallback=True)
        self.assertEqual((code, llm2.calls, self.name_of('b')), (1, [], LONG_TITLE))
        code, _ = self.go('names', llm=llm2, allow_no_bookmark=True, llm_fallback=True)
        self.assertEqual((code, self.name_of('b')), (0, '山田屋'))
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
        llm = FakeLLM(rewrite={PLAIN_LLM: '山田氏が2020年に始めた活動を記録します。「ひだまり」の様子を載せました。',
                               '作品を載せた。': '作品を載せた内容に新しい発表が3件あります。'})
        code, st = self.go('descriptions', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.desc('good'), '家具工房の紹介サイト。')
        self.assertEqual(self.desc('rule'), '手作りの家具を作っています。')
        self.assertEqual(self.desc('llm'), '山田氏が2020年に始めた活動を記録します。「ひだまり」の様子を載せました。')
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
        self.assertEqual(ok('作品を紹介している。ご注文は受け付けない。', '作品を紹介しています。ご注文は受け付けません。'), (True, ''))
        self.assertEqual(ok('作品を載せた。', '作品を載せた。')[0], False)
        self.assertEqual(ok('Linuxの話を書いた。', 'Windowsの話を書きました。')[0], False)
        self.assertEqual(ok('2020年に始めた。', '2021年に始めました。')[0], False)
        self.assertEqual(ok('「ひだまり」を載せた。', 'ひなたを載せました。')[0], False)
        self.assertEqual(ok('作品を載せた。', '作品を載せましたが、それ以外にも様々な活動の記録を毎週詳しく丁寧に載せています。'), (False, 'length'))
        self.assertFalse(ok('作品を載せた。', None)[0])

    def test_check_rewrite_rejects_meaning_changes(self):
        bad = [('公式の団体ではない。', '公式の団体です。'), ('作品を販売しない。', '作品を販売します。'), ('無料で使える。', '有料で使えます。'),
               ('東京で開催する。', '大阪で開催します。'), ('事業を終了した。', '事業を開始しました。'), ('料理を提供している。', '絶品の料理を提供しています。'),
               ('日本のお店を紹介している。', '日本最大級のお店を紹介しています。'), ('人気のお店を紹介している。', 'お店を紹介しています。'),
               ('作品を載せた。サイトを運営している。', '作品を載せました。')]
        for old, new in bad:
            self.assertFalse(cq.check_rewrite(old, new)[0], (old, new))

    def test_rule_path_is_checked_and_keeps_spaces(self):
        add_site(self.d1, 'sp', '山田屋', desc='作品を作っている。  家具も売っている。')
        add_site(self.d1, 'hira', '山田屋', desc='また、あした。')
        add_site(self.d1, 'hira2', '山田屋', desc='家具を作っている。また、あした')
        llm = FakeLLM()
        self.go('descriptions', llm=llm)
        self.assertEqual(self.desc('sp'), '作品を作っています。  家具も売っています。')
        self.assertEqual(self.desc('hira'), 'また、あした。')
        self.assertEqual(self.desc('hira2'), '家具を作っている。また、あした')

    def desc(self, sid):
        return self.d1.one('SELECT description_ja FROM sites WHERE id=?', (sid,))[0]


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
