"""Quality rules inside the stages (Issue #273): judge retry / hold, describe-after-approve, publish gate repair / hold. Fake D1 + fake LLM only."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
sys.path.insert(0, str(Path(__file__).parent))
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

    def test_unrepairable_name_is_held_never_cut(self):
        cand(self.d1, title='あ' * 70)
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


if __name__ == '__main__':
    unittest.main()
