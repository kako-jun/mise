"""Quality rules inside the stages (Issue #273): judge retry / hold, describe-after-approve, publish gate repair / hold. Fake D1 + fake LLM only."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import judge_stage as js  # noqa: E402
import publish_stage as ps  # noqa: E402
import test_enrich_judge_stage as tj  # noqa: E402
import test_enrich_publish_stage as tp  # noqa: E402

RETRY_MARK = 'PREVIOUS ANSWER WAS REJECTED'


class ScriptedLLM(tj.FakeLLM):
    """The normal fake, then one field of every item is replaced. bad[(stage, field)] = (first answer, answer after the violation is stated);
    the second is None to keep the first one (an answer that never gets fixed)."""

    def __init__(self, bad):
        super().__init__()
        self.bad = bad

    def __call__(self, params):
        msg = super().__call__(params)
        text = params['messages'][0]['content']
        stage = 'triage' if 'STAGE 1' in params['system'][0]['text'] else 'describe'
        retry = RETRY_MARK in text
        try:
            obj = json.loads(msg['content'][0]['text'])
        except ValueError:
            return msg
        for it in obj['r']:
            for (st, field), (first, second) in self.bad.items():
                if st == stage:
                    it[field] = second if retry and second is not None else first
        msg['content'][0]['text'] = json.dumps(obj, ensure_ascii=False)
        return msg


class JudgeQuality(unittest.TestCase):
    def setUp(self):
        self.d1, self.out = tj.FakeD1(), []

    def go(self, llm):
        self.llm = llm
        return js.run(self.d1, limit=10, apply=True, log=self.out.append, llm=llm)

    def describe_calls(self):
        return [c for c in self.llm.calls if 'STAGE 2' in c['system'][0]['text']]

    def test_prompts_carry_the_rules(self):
        ctx = js.Ctx(None, None, print, js.judge.load_categories())
        self.assertIn('体言止め', ctx.systems['describe'])
        self.assertIn('日本語ラベル|english label', ctx.systems['describe'])
        self.assertIn('LEAF', ctx.systems['triage'])
        self.assertNotIn('LEAF', js.llm_pipeline.triage_system(js.judge.load_categories()))     # the old pipeline's prompt is unchanged
        self.assertNotIn('体言止め', js.llm_pipeline.describe_system(js.judge.load_categories()))

    def test_plain_description_is_retried_with_the_violation_then_accepted(self):
        self.d1.add('a.example')
        s = self.go(ScriptedLLM({('describe', 'dj'): ('作品を紹介している。', '作品を紹介しています。')}))
        self.assertEqual(self.d1.state('a.example'), 'approved')
        self.assertEqual(self.d1.cand('a.example')['description_ja'], '作品を紹介しています。')
        self.assertEqual((s['quality_retries'], s['quality_held']), (1, 0))
        retry = self.describe_calls()[-1]['messages'][0]['content']
        self.assertIn(RETRY_MARK, retry)
        self.assertIn('quality:description', retry)

    def test_description_never_fixed_goes_to_candidate_with_the_reason(self):
        self.d1.add('a.example')
        s = self.go(ScriptedLLM({('describe', 'dj'): ('作品を紹介している。', None)}))
        self.assertEqual(self.d1.state('a.example'), 'candidate')
        c = self.d1.cand('a.example')
        self.assertEqual((c['review_state'], c['review_reason']), ('pending', 'quality:description'))
        self.assertEqual((s['quality_retries'], s['quality_held'], s['candidate']), (1, 1, 1))
        self.assertEqual(len(self.describe_calls()), 2)                 # exactly one retry

    def test_english_tags_are_a_violation(self):
        self.d1.add('a.example')
        s = self.go(ScriptedLLM({('describe', 'tg'): (['craft'], None)}))
        self.assertEqual(self.d1.cand('a.example')['review_reason'], 'quality:tags')
        self.assertEqual(s['quality_held'], 1)

    def test_duplicate_tags_are_a_violation_and_fixable(self):
        self.d1.add('a.example')
        self.go(ScriptedLLM({('describe', 'tg'): (['動画|video', '映像|videos'], ['動画|video', '音楽|music'])}))
        self.assertEqual(self.d1.state('a.example'), 'approved')
        self.assertEqual(json.loads(self.d1.cand('a.example')['tags']), ['動画|video', '音楽|music'])

    def test_non_leaf_category_retried_then_held_without_describe(self):
        self.d1.add('a.example')
        self.d1.add('b.example')
        s = self.go(ScriptedLLM({('triage', 'cat'): (['reference'], None)}))
        for k in ('a.example', 'b.example'):
            self.assertEqual(self.d1.state(k), 'candidate')
            self.assertEqual(self.d1.cand(k)['review_reason'], 'quality:categories')
        self.assertEqual((s['quality_held'], s['quality_retries']), (2, 2))
        self.assertEqual(self.describe_calls(), [])                      # no money spent on a describe for a row a human must look at

    def test_retry_note_is_outside_the_candidate_block(self):
        self.d1.add('a.example')
        self.go(ScriptedLLM({('describe', 'dj'): ('作品を紹介している。', '作品を紹介しています。')}))
        t = self.describe_calls()[-1]['messages'][0]['content']
        self.assertLess(t.index(RETRY_MARK), t.index('<candidate'))

    def test_stale_reject_is_not_overwritten_by_a_quality_hold(self):
        # decide() says rejected (REJECT_STALE) -> the hold must not turn it into pending
        orig = js.decide
        js.decide = lambda row: ('rejected', 'stale_individual_x', 'rejected')
        self.addCleanup(setattr, js, 'decide', orig)
        self.d1.add('a.example')
        self.go(ScriptedLLM({('describe', 'dj'): ('作品を紹介している。', None)}))
        self.assertEqual((self.d1.state('a.example'), self.d1.cand('a.example')['review_state']), ('rejected', 'rejected'))

    def test_non_leaf_category_fixed_by_the_retry(self):
        self.d1.add('a.example')
        self.go(ScriptedLLM({('triage', 'cat'): (['reference'], [tj.CAT])}))
        self.assertEqual(self.d1.state('a.example'), 'approved')

    def test_plain_polite_and_noun_endings_pass_without_retry(self):
        for i, d in enumerate(('小さな工房のサイト。', '作品を公開しています。')):
            self.d1.add(f'k{i}.example')
        s = self.go(ScriptedLLM({('describe', 'dj'): ('作品を公開しています。', None)}))
        self.assertEqual((s['quality_retries'], s['approved']), (0, 2))


def cand(d1, key='alpha.example.jp', **kw):
    tp.add_candidate(d1, key, **kw)


class PublishGate(unittest.TestCase):
    def setUp(self):
        self.d1, self.logs = tp.FakeD1(), []

    def run_stage(self, apply=True):
        return ps.run(self.d1, limit=50, apply=apply, log=self.logs.append)

    def reason(self, key):
        return self.d1.one('SELECT review_reason FROM url_candidates WHERE identity_key=?', (key + '/',))[0]

    def test_clean_row_publishes_unrepaired(self):
        cand(self.d1)
        res = self.run_stage()
        self.assertEqual((res['published'], res['repaired']), (1, {'name': 0, 'description': 0}))

    def test_long_title_is_repaired_deterministically(self):
        cand(self.d1, 'infigo.example.jp', host='infigo.example.jp',
             title='Infigo online｜新感覚アートブランドが提案する、美しい発色のグラフィックアイテム｜インフィゴのオンラインショップ')
        res = self.run_stage()
        self.assertEqual((res['published'], res['held'], res['repaired']['name']), (1, 0, 1))
        self.assertEqual(self.d1.one('SELECT name FROM sites WHERE id=?', (ps.site_id('infigo.example.jp'),))[0], 'Infigo online')

    def test_long_single_clause_name_is_kept(self):
        cand(self.d1, title='あ' * 52)
        res = self.run_stage()
        self.assertEqual((res['published'], res['repaired']['name']), (1, 0))
        self.assertEqual(self.d1.one('SELECT name FROM sites WHERE id=?', (ps.site_id('alpha.example.jp'),))[0], 'あ' * 52)

    def test_llm_site_name_is_used_when_the_title_cannot_be_repaired(self):
        cand(self.d1, title='ホーム｜新着情報｜お問い合わせ｜アクセス', site_name='あおぞら工房')
        res = self.run_stage()
        self.assertEqual(res['published'], 1)
        self.assertEqual(self.d1.one('SELECT name FROM sites WHERE id=?', (ps.site_id('alpha.example.jp'),))[0], 'あおぞら工房')
        cand(self.d1, 'beta.example.jp', title='ホーム｜新着情報｜お問い合わせ｜アクセス', site_name='あ' * 41)       # an invalid LLM name is not used
        self.assertEqual(self.run_stage()['held_reasons'], {'gate:quality_name': 1})

    def test_unrepairable_name_is_held_never_cut(self):
        cand(self.d1, title='あ' * 90)
        res = self.run_stage()
        self.assertEqual((res['published'], res['held']), (0, 1))
        self.assertEqual(self.reason('alpha.example.jp'), 'gate:quality_name')
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='alpha.example.jp/'")[0], 'candidate')

    def test_plain_description_is_repaired_to_polite(self):
        cand(self.d1, description_ja='作品を紹介している。')
        res = self.run_stage()
        self.assertEqual((res['published'], res['repaired']['description']), (1, 1))
        self.assertEqual(self.d1.one('SELECT description_ja FROM sites WHERE id=?', (ps.site_id('alpha.example.jp'),))[0], '作品を紹介しています。')

    def test_unsafe_plain_description_is_held(self):
        cand(self.d1, description_ja='本を読んだ。')
        res = self.run_stage()
        self.assertEqual((res['published'], res['held']), (0, 1))
        self.assertEqual(self.reason('alpha.example.jp'), 'gate:quality_description')

    def test_english_tags_are_held(self):
        cand(self.d1, tags=json.dumps(['Cats', 'dogs']))
        res = self.run_stage()
        self.assertEqual(res['held_reasons'], {'gate:quality_tags': 1})
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites WHERE url LIKE ?', ('%alpha.example.jp%',))[0], 0)

    def test_near_duplicate_tags_are_held(self):
        cand(self.d1, tags=json.dumps(['動画|video', '映像|videos']))
        self.assertEqual(self.run_stage()['held_reasons'], {'gate:quality_tags': 1})

    def test_tag_labels_are_stored_separately(self):
        cand(self.d1, tags=json.dumps(['動画|video', {'label_ja': '猫', 'label_en': 'cat'}]))
        self.run_stage()
        rows = self.d1.db.execute('SELECT label_ja,label_en FROM site_tags WHERE site_id=? ORDER BY label_ja', (ps.site_id('alpha.example.jp'),)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [('動画', 'video'), ('猫', 'cat')])

    def test_non_leaf_category_is_held(self):
        cand(self.d1, categories=json.dumps(['reference']))
        res = self.run_stage()
        self.assertEqual(res['held_reasons'], {'gate:quality_categories': 1})

    def test_dry_run_counts_repairs_and_writes_nothing(self):
        cand(self.d1, description_ja='作品を紹介している。')
        w = self.d1.writes
        res = self.run_stage(apply=False)
        self.assertEqual((res['would_publish'], res['repaired']['description'], self.d1.writes), (1, 1, w))

    def test_gate_reason_is_the_same_function_for_the_deliberation(self):
        row = {'identity_key': 'x/', 'frontier_key': 'x.example.jp', 'host': 'x.example.jp', 'url': 'https://x.example.jp/', 'title': 'X',
               'description_ja': '工房の紹介。', 'description_en': 'A workshop.', 'categories': json.dumps([tp.LEAF]), 'tags': json.dumps(['工芸|craft']),
               'spam_verdict': 'pass', 'adult': 'no'}
        self.assertIsNone(ps.gate_reason(row))
        self.assertEqual(ps.gate_reason(dict(row, tags=json.dumps(['craft']))), 'gate:quality_tags')
        self.assertEqual(ps.gate_reason(dict(row, categories=json.dumps(['reference']))), 'gate:quality_categories')


class StuckLLM(tj.FakeLLM):
    """The normal fake; describe answers for urls containing 'stuck' keep English tags, 'name' urls carry the site name 'あおぞら工房'."""

    def __call__(self, params):
        msg = super().__call__(params)
        text = params['messages'][0]['content']
        if 'STAGE 2' not in params['system'][0]['text']:
            return msg
        urls = __import__('re').findall(r'url: (\S+)', text)
        obj = json.loads(msg['content'][0]['text'])
        for u, it in zip(urls, obj['r']):
            if 'stuck' in u:
                it['tg'] = ['craft']
            if 'name' in u:
                it['sn'] = 'あおぞら工房'
        msg['content'][0]['text'] = json.dumps(obj, ensure_ascii=False)
        return msg


class Requalify(unittest.TestCase):
    def setUp(self):
        self.d1, self.logs = tp.FakeD1(), []
        self.llm = StuckLLM()

    def rq(self, limit=5):
        return js.requalify(self.d1, limit=limit, apply=True, log=self.logs.append, llm=self.llm)

    def row(self, key):
        return self.d1.one("SELECT p.state AS st, c.review_state AS rs, c.review_reason AS why, c.notes AS notes, c.tags AS tags, c.site_name AS sn, c.categories AS cats "
                           "FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.identity_key=?", (key + '/',))

    def test_english_only_tags_get_japanese_labels_and_publish(self):
        cand(self.d1, tags=json.dumps(['craft']))
        s = self.rq()
        self.assertEqual((s['selected'], s['requalified'], s['still_held']), (1, 1, 0))
        r = self.row('alpha.example.jp')
        self.assertEqual((r['st'], r['rs'], r['why'], json.loads(r['tags'])), ('approved', 'approved', 'requalified', ['工芸|craft']))
        res = ps.run(self.d1, limit=5, apply=True, log=self.logs.append)
        self.assertEqual((res['published'], res['held']), (1, 0))
        self.assertEqual([tuple(x) for x in self.d1.db.execute('SELECT label_ja,label_en FROM site_tags WHERE site_id=?', (ps.site_id('alpha.example.jp'),))], [('工芸', 'craft')])

    def test_held_rows_of_the_gate_and_of_the_judge_are_requalified(self):
        cand(self.d1, tags=json.dumps(['craft']), state='candidate', review_state='pending', review_reason='gate:quality_tags')
        cand(self.d1, 'b.example.jp', description_ja='作品を紹介している。', state='candidate', review_state='pending', review_reason='quality:description')
        s = self.rq()
        self.assertEqual((s['selected'], s['requalified']), (2, 2))
        self.assertEqual({self.row(k)['st'] for k in ('alpha.example.jp', 'b.example.jp')}, {'approved'})

    def test_name_that_cannot_be_repaired_gets_the_llm_name(self):
        cand(self.d1, 'name.example.jp', title='ホーム｜新着情報｜お問い合わせ｜アクセス', text_head='あおぞら工房へようこそ。木の家具を作っています。')
        s = self.rq()
        self.assertEqual(s['requalified'], 1, self.logs)
        self.assertEqual(self.row('name.example.jp')['sn'], 'あおぞら工房')
        self.assertEqual(ps.run(self.d1, limit=5, apply=True, log=self.logs.append)['published'], 1)
        self.assertEqual(self.d1.one('SELECT name FROM sites WHERE id=?', (ps.site_id('name.example.jp'),))[0], 'あおぞら工房')

    def test_non_leaf_categories_get_a_leaf(self):
        cand(self.d1, categories=json.dumps(['reference']))
        self.assertEqual(self.rq()['requalified'], 1)
        self.assertEqual(json.loads(self.row('alpha.example.jp')['cats']), [tj.CAT])

    def test_row_that_cannot_be_fixed_stays_with_a_reason_and_never_blocks_the_queue(self):
        cand(self.d1, 'a-stuck.example.jp', tags=json.dumps(['craft']))
        cand(self.d1, 'b.example.jp', tags=json.dumps(['craft']))
        s = self.rq(limit=1)                                     # the stuck row is first (same updated_at, key order)
        self.assertEqual((s['selected'], s['requalified'], s['still_held'], s['describe_failed']), (1, 0, 1, 1))
        r = self.row('a-stuck.example.jp')
        self.assertEqual((r['st'], r['rs'], r['why']), ('candidate', 'pending', 'gate:quality_tags'))
        self.assertIn('requalify_attempts=1', r['notes'])
        s = self.rq(limit=1)                                     # the next run reaches the row behind it
        self.assertEqual(self.row('b.example.jp')['st'], 'approved')
        self.rq(limit=1)
        self.assertEqual(self.row('a-stuck.example.jp')['notes'].count('requalify_attempts=2'), 1)
        self.assertEqual(self.rq(limit=5)['selected'], 0)        # attempts used up: only a human moves it now

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        cand(self.d1, tags=json.dumps(['craft']))
        w = self.d1.writes
        s = js.requalify(self.d1, limit=5, apply=False, log=self.logs.append, llm=self.llm)
        self.assertEqual((s['selected'], self.llm.calls, self.d1.writes), (1, [], w))

    def test_clean_approved_rows_are_not_selected(self):
        cand(self.d1)
        self.assertEqual(self.rq()['selected'], 0)


class Wiring(unittest.TestCase):
    def test_runner_has_the_requalify_stage_before_publish(self):
        import enrich_online as eo
        self.assertEqual(eo.STAGES.index('requalify') + 1, eo.STAGES.index('publish'))
        self.assertEqual(eo.DEFAULT_LIMITS['requalify'], 20)

    def test_deliberation_handles_the_quality_gates_and_leaf_categories(self):
        import deliberate_stage as ds
        self.assertTrue({'gate:quality_name', 'gate:quality_description', 'gate:quality_tags', 'gate:quality_categories'} <= ds.DESCRIBABLE)
        self.assertTrue(ds.categories_usable({'categories': json.dumps([tp.LEAF])}))
        self.assertFalse(ds.categories_usable({'categories': json.dumps(['reference'])}))
        self.assertFalse(ds.categories_usable({'categories': json.dumps([])}))

    def test_describe_row_triage_path_sees_the_quality_hold(self):
        d1 = tj.FakeD1()
        d1.add('a.example')
        ctx = js.Ctx(ScriptedLLM({('triage', 'cat'): (['reference'], None)}), js.Ledger(d1, 'r', 100.0), print, js.judge.load_categories())
        row = dict(d1.query('SELECT p.identity_key, c.* FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key')[0])
        out, problem = js.describe_row(ctx, row, need_categories=True)
        self.assertEqual((out, problem), (None, 'quality:categories'))

    def test_prompt_overrides_the_parent_category_fallback(self):
        ctx = js.Ctx(None, None, print, js.judge.load_categories())
        self.assertIn('overrides', ctx.systems['triage'])


if __name__ == '__main__':
    unittest.main()
