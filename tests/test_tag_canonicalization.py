"""Tag reuse (#273): quality.canonical_tag / canonicalize_tags / VocabCache, their use in the judge and publish stages, and
`cleanup_quality.py tags --merge`. Deterministic: no LLM for the matching. Fake D1 = in-memory SQLite built from the repo's real migrations. No network,
no production D1, no files."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
for sub in ('scripts', 'scripts/llm', 'tests'):
    sys.path.insert(0, str(ROOT / sub))
import cleanup_quality as cq
from enrich import judge_stage as js
from enrich import publish_stage as ps
from enrich import quality
import test_enrich_judge_stage as tj
import test_enrich_publish_stage as tp
import test_enrich_quality_stages as tq
from test_cleanup_quality import Base, add_site
from test_cleanup_tags_categories import add_tags, tags_of

VOCAB = [('動画', 'video', 500), ('大学', 'university', 300), ('同窓会', 'alumni association', 40), ('非営利', 'non-profit', 30), ('営利', 'profit', 10),
         ('サーバー', 'server', 20), ('銀行', 'bank', 100), ('土手', 'bank', 3), ('FPGA', 'fpga', 5), ('子供', 'children', 50), ('保育', 'childcare', 20),
         ('管理者', 'manager', 9), ('管理済み', 'managed', 9), ('3Dプリント', '3d printing', 7), ('写真', 'photography', 80), ('映像', 'footage', 60),
         ('university', 'university', 900)]          # the last one is a tag whose label_ja is English: never an answer


class Canonical(unittest.TestCase):
    def setUp(self):
        self.v = quality.TagVocab(VOCAB)

    def c(self, ja, en):
        return quality.canonical_tag(ja, en, self.v)

    def test_english_key_plural_case_articles_hyphen(self):
        self.assertEqual(self.c('ビデオ', 'videos'), ('動画', 'video'))
        self.assertEqual(self.c('動画', 'The Video'), ('動画', 'video'))
        self.assertEqual(self.c('大学', 'universities'), ('大学', 'university'))
        self.assertEqual(self.c('非営利', 'nonprofit'), ('非営利', 'non-profit'))
        self.assertEqual(self.c('3Dプリント', '3D-Printing'), ('3Dプリント', '3d printing'))

    def test_a_vocabulary_entry_with_an_english_label_ja_is_never_the_answer(self):
        self.assertEqual(self.c('大学教育', 'university'), ('大学', 'university'))     # 'university / university' (900 rows) has the most uses but cannot be published

    def test_japanese_key_width_long_vowel_and_no(self):
        self.assertEqual(self.c('サーバ', 'servers hosting'), ('サーバー', 'server'))
        self.assertEqual(self.c('ｻｰﾊﾞｰ', 'x'), ('サーバー', 'server'))                    # half width katakana
        self.assertEqual(self.c('動画の', 'x'), ('動画', 'video'))                        # trailing の
        self.assertIsNone(self.c('子供用', 'for kids'))                                  # 用 is not folded: 子供用 is not 子供

    def test_synonym_table_both_directions(self):
        self.assertEqual(self.c('ビデオ', 'zzz clip'), ('動画', 'video'))                 # variant -> the most used existing tag of the group
        v2 = quality.TagVocab([('映像', 'footage', 60), ('写真', 'photography', 80)])
        self.assertEqual(quality.canonical_tag('動画', 'clips', v2), ('映像', 'footage'))       # canonical -> an existing variant of its group
        self.assertEqual(quality.canonical_tag('フォト', 'zzz', v2), ('写真', 'photography'))
        v3 = quality.TagVocab([('猫', 'cat', 80)])
        self.assertEqual(quality.canonical_tag('ビデオ', 'zzz clip', v3), ('動画', 'video'))     # nothing of the group stored: the table's own tag
        self.assertEqual(self.c('映像', 'movie clip'), ('映像', 'footage'))                    # the Japanese key (an existing tag) comes before the table

    def test_word_order_and_typo(self):
        self.assertEqual(self.c('同窓会', 'association alumni'), ('同窓会', 'alumni association'))
        self.assertEqual(self.c('x', 'universtiy'), ('大学', 'university'))               # transposition
        self.assertEqual(self.c('x', 'univeristy'), ('大学', 'university'))
        self.assertEqual(self.c('x', 'alumni associaton'), ('同窓会', 'alumni association'))

    def test_fuzzy_never_on_short_words_known_words_or_other_word_forms(self):
        v = quality.TagVocab([('猫', 'cat', 50), ('管理者', 'manager', 9), ('美術', 'painting', 9), ('組合', 'association', 20)])
        self.assertIsNone(quality.canonical_tag('x', 'cut', v))                          # short word
        self.assertIsNone(quality.canonical_tag('x', 'managed', v))                      # another form of the word, not a typo
        self.assertIsNone(quality.canonical_tag('x', 'pointing', v))                     # first letters differ: another word
        self.assertEqual(quality.canonical_tag('x', 'paintng', v), ('美術', 'painting'))
        self.assertEqual(quality.canonical_tag('x', 'assocation', v), ('組合', 'association'))
        v2 = quality.TagVocab([('管理者', 'manager', 9), ('管理済み', 'managed', 9)])
        self.assertIsNone(quality.canonical_tag('x', 'managers x', v2))

    def test_negation_and_qualifier_never_merge(self):
        self.assertEqual(self.c('営利', 'profit'), ('営利', 'profit'))
        self.assertEqual(self.c('営利団体', 'non profit'), ('非営利', 'non-profit'))
        self.assertIsNone(self.c('x', 'childcares x'))
        self.assertEqual(self.c('保育', 'childcare'), ('保育', 'childcare'))             # children / childcare stay two tags
        self.assertEqual(self.c('子供', 'children'), ('子供', 'children'))
        v = quality.TagVocab([('活動', 'active member', 50)])
        self.assertIsNone(quality.canonical_tag('x', 'inactive member', v))              # a prefix that negates is never a typo
        v2 = quality.TagVocab([('営利', 'profit organization', 50)])
        self.assertIsNone(quality.canonical_tag('x', 'non profit organization', v2))     # 'non' is a word the other label lacks

    def test_the_two_labels_disagree_is_a_new_tag(self):
        self.assertIsNone(self.c('非営利', 'profit'))                                     # ja says 非営利, en says profit

    def test_proper_nouns_exact_only(self):
        self.assertEqual(self.c('FPGA', 'fpga'), ('FPGA', 'fpga'))
        self.assertEqual(self.c('fpga', 'FPGA'), ('FPGA', 'fpga'))                        # case only
        self.assertIsNone(self.c('FPGAs', 'fpgas'))
        v = quality.TagVocab([('エフピージーエー', 'fpgas', 5), ('5G', '5g', 9)])
        self.assertIsNone(quality.canonical_tag('5Gs', '5gs', v))
        self.assertEqual(quality.canonical_tag('5G', '5g', v), ('5G', '5g'))

    def test_one_english_label_two_unrelated_japanese_labels(self):
        self.assertEqual(self.c('銀行', 'bank'), ('銀行', 'bank'))                          # 100 vs 3: a clear majority
        v = quality.TagVocab([('銀行', 'bank', 10), ('土手', 'bank', 8)])
        self.assertIsNone(quality.canonical_tag('x', 'bank', v))                         # a real tie of senses: nothing is guessed

    def test_ties_go_to_the_highest_use_count_then_the_shorter_label_then_alphabetical(self):
        v = quality.TagVocab([('動画', 'video', 5), ('映像', 'video', 9)])
        self.assertEqual(quality.canonical_tag('ムービー', 'video', v), ('映像', 'video'))   # same group, more uses
        v = quality.TagVocab([('動画', 'video', 5), ('映像', 'video', 5)])
        self.assertEqual(quality.canonical_tag('ムービー', 'video', v), ('動画', 'video'))   # equal uses, equal length: alphabetical
        v = quality.TagVocab([('絵', 'picture', 9), ('画', 'picture', 3)])
        self.assertEqual(quality.canonical_tag('作品', 'picture', v), ('絵', 'picture'))     # unrelated labels: the dominant one (75%)

    def test_list_vocabulary_is_indexed_once(self):
        n = []
        orig = quality.TagVocab.add
        quality.TagVocab.add = lambda self, *a, **k: (n.append(1), orig(self, *a, **k))[1]
        try:
            for _ in range(5):
                quality.canonical_tag('ビデオ', 'videos', VOCAB)
        finally:
            quality.TagVocab.add = orig
        self.assertEqual(len(n), len(VOCAB))

    def test_pure_and_deterministic(self):
        a = [quality.canonical_tag(j, e, VOCAB) for j, e in [('ビデオ', 'videos'), ('x', 'universtiy'), ('新', 'brand new')]]
        b = [quality.canonical_tag(j, e, list(reversed(VOCAB))) for j, e in [('ビデオ', 'videos'), ('x', 'universtiy'), ('新', 'brand new')]]
        self.assertEqual(a, b)
        self.assertIsNone(a[2])


class Canonicalize(unittest.TestCase):
    def setUp(self):
        self.v = quality.TagVocab(VOCAB)

    def test_new_tags_are_capped_at_two_in_the_llms_order(self):
        got, st = quality.canonicalize_tags(['新一|new one', 'ビデオ|videos', '新二|new two', '新三|new three', '大学|university'], self.v)
        self.assertEqual(got, [('新一', 'new one'), ('動画', 'video'), ('新二', 'new two'), ('大学', 'university')])
        self.assertEqual((st['new'], st['dropped'], st['canonicalized'], st['reused']), (2, 1, 1, 1))

    def test_dedupe_and_five_at_most(self):
        got, st = quality.canonicalize_tags(['動画|video', 'ビデオ|videos', '映像|footage', '大学|university', '銀行|bank', '写真|photography',
                                             '子供|children', '保育|childcare'], self.v)
        self.assertEqual(len(got), 5)
        self.assertEqual(len({a for a, _b in got}), 5)
        self.assertGreaterEqual(st['dropped'], 3)

    def test_new_tags_still_pass_check_tags_or_the_original_is_kept(self):
        got, st = quality.canonicalize_tags(['english|english', '動画|video'], self.v)         # a label_ja that is not Japanese was never valid
        self.assertEqual(st['fallback'], 1)
        self.assertEqual(got, [('english', 'english'), ('動画', 'video')])
        got, st = quality.canonicalize_tags(['新しい|brand new'], self.v)
        self.assertEqual((got, st['new'], st['fallback']), ([('新しい', 'brand new')], 1, 0))
        self.assertEqual(quality.check_tags([{'label_ja': a, 'label_en': b} for a, b in got]), [])


class FakeCounting:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query(self, sql, params=()):
        self.queries.append(sql)
        return self.rows


class Cache(unittest.TestCase):
    ROWS = [{'label_ja': a, 'label_en': b, 'n': n} for a, b, n in VOCAB]

    def test_one_query_then_a_reload_every_n_rows(self):
        c = FakeCounting(self.ROWS)
        cache = quality.VocabCache(c, refresh_rows=3, min_size=1)
        for _ in range(7):
            quality.canonicalize_strings(['ビデオ|videos'], cache, {})
        self.assertEqual(len(c.queries), 3)                                           # 7 rows, a reload at row 3 and 6: never one per row
        self.assertIn('GROUP BY', c.queries[0])
        self.assertEqual(quality.VOCAB_SQL.count('?'), 1)

    def test_no_vocabulary_means_tags_pass_unchanged(self):
        class Boom:
            def query(self, *a):
                raise RuntimeError('d1 down')
        for client in (Boom(), FakeCounting([])):
            cache = quality.VocabCache(client)
            self.assertEqual(quality.canonicalize_strings(['新|new', '別|other', '三|three'], cache, {}), ['新|new', '別|other', '三|three'])
        small = quality.VocabCache(FakeCounting(self.ROWS))                                 # fewer than VOCAB_MIN tags: no basis for reuse
        self.assertIsNone(small.vocab())

    def test_stats_names(self):
        st = {}
        quality.canonicalize_strings(['ビデオ|videos', '新|brand new'], quality.VocabCache(FakeCounting(self.ROWS), min_size=1), st)
        self.assertEqual((st['tags_canonicalized'], st['tags_new'], st['tags_dropped']), (1, 1, 0))


class TagD1(tj.FakeD1):
    """The judge's fake D1 plus a site_tags table that holds a real-sized vocabulary."""
    def __init__(self):
        super().__init__()
        self.db.execute('CREATE TABLE site_tags (site_id TEXT NOT NULL, label_ja TEXT NOT NULL, label_en TEXT, PRIMARY KEY (site_id, label_ja))')
        rows = [('s%d' % i, '種類%d' % i, 'kind%d word' % i) for i in range(60)] + [('sv%d' % i, '動画', 'video') for i in range(5)]
        self.db.executemany('INSERT INTO site_tags VALUES (?,?,?)', rows)


class JudgeIntegration(unittest.TestCase):
    def run_judge(self, tags, **kw):
        d1 = TagD1()
        d1.add('a.example')
        llm = tq.ScriptedLLM({('describe', 'tg'): (tags, None)})
        s = js.run(d1, limit=5, apply=True, log=lambda *_: None, llm=llm, **kw)
        return d1, s

    def test_the_llms_tags_are_mapped_onto_existing_tags_and_new_ones_capped(self):
        d1, s = self.run_judge(['ビデオ|videos', '新一|new one', '新二|new two', '新三|new three', '種類3|kind3 words'])
        stored = json.loads(d1.cand('a.example')['tags'])
        self.assertEqual(stored, ['動画|video', '新一|new one', '新二|new two', '種類3|kind3 word'])
        self.assertEqual((s['tags_canonicalized'], s['tags_new'], s['tags_dropped']), (2, 2, 1))
        self.assertEqual(quality.check_tags(stored), [])

    def test_one_vocabulary_query_per_run_not_per_row(self):
        d1 = TagD1()
        for i in range(4):
            d1.add(f'r{i}.example')
        js.run(d1, limit=10, apply=True, log=lambda *_: None, llm=tq.ScriptedLLM({('describe', 'tg'): (['ビデオ|videos'], None)}))
        self.assertEqual(sum(1 for q in d1.log if 'FROM site_tags GROUP BY' in q), 1)

    def test_without_a_site_tags_vocabulary_tags_pass_unchanged(self):
        d1 = tj.FakeD1()                                   # no site_tags table at all: the load fails, the run goes on
        d1.add('a.example')
        s = js.run(d1, limit=5, apply=True, log=lambda *_: None, llm=tq.ScriptedLLM({('describe', 'tg'): (['ビデオ|videos', '新|a b', '二|c d', '三|e f'], None)}))
        self.assertEqual(json.loads(d1.cand('a.example')['tags']), ['ビデオ|videos', '新|a b', '二|c d', '三|e f'])
        self.assertEqual(s['tags_new'], 0)

    def test_vocabulary_in_prompt_is_off_by_default_and_on_by_flag(self):
        d1 = TagD1()
        ctx = js.attach_tag_vocab(js.Ctx(None, None, print, js.judge.load_categories()), d1)
        self.assertNotIn('EXISTING TAGS', ctx.systems['describe'])
        js.TAG_VOCAB_IN_PROMPT = True
        try:
            ctx = js.attach_tag_vocab(js.Ctx(None, None, print, js.judge.load_categories()), d1)
        finally:
            js.TAG_VOCAB_IN_PROMPT = False
        self.assertIn('EXISTING TAGS', ctx.systems['describe'])
        self.assertIn('動画|video', ctx.systems['describe'])


class PublishIntegration(unittest.TestCase):
    def test_publish_reuses_existing_tags_and_counts(self):
        d1 = tp.FakeD1()
        for i in range(60):                                              # a vocabulary of our own on top of the dev seed
            sid = 'tagsite%d' % i
            d1.db.execute('INSERT INTO sites (id,url,name,description_ja,description_en) VALUES (?,?,?,?,?)', (sid, f'https://{sid}.example.jp/', 'n', 'あ。', 'a'))
            d1.db.execute('INSERT INTO site_tags VALUES (?,?,?)', (sid, '語彙%d' % i, 'vocab%d term' % i))
        tp.add_candidate(d1, tags=json.dumps(['語彙3|vocabs3 term', '新一|new one', '新二|new two', '新三|new three']))
        logs = []
        res = ps.run(d1, limit=5, apply=True, log=logs.append)
        self.assertEqual((res['published'], res['errors']), (1, 0))
        got = sorted(tuple(r) for r in d1.db.execute('SELECT label_ja,label_en FROM site_tags WHERE site_id=?', (ps.site_id('alpha.example.jp'),)))
        self.assertEqual(got, sorted([('語彙3', 'vocab3 term'), ('新一', 'new one'), ('新二', 'new two')]))
        self.assertEqual((res['tags_canonicalized'], res['tags_new'], res['tags_dropped']), (1, 2, 1))
        self.assertEqual(res['integrity'], {})

    def test_dry_run_counts_the_same_and_writes_nothing(self):
        d1 = tp.FakeD1()
        tp.add_candidate(d1, tags=json.dumps(['動画|video', '猫|cat']))
        before = d1.writes
        res = ps.run(d1, limit=5, apply=False, log=lambda *_: None)
        self.assertEqual((res['would_publish'], d1.writes), (1, before))


class Merge(Base):
    def seed(self):
        for sid in ('s1', 's2', 's3', 's4', 's5', 's6'):
            add_site(self.d1, sid, '山田屋')
        add_tags(self.d1, 's1', ('動画', 'video'), ('猫', 'cat'))
        add_tags(self.d1, 's2', ('動画', 'video'), ('犬', 'dog'))
        add_tags(self.d1, 's3', ('動画', 'video'), ('動画の', 'videos'))                        # same site holds both: collision
        add_tags(self.d1, 's4', ('ビデオ', 'videos'), ('大学', 'universities'))
        add_tags(self.d1, 's5', ('大学', 'university'), ('映像', 'video'), ('university', 'university'), ('銀行', 'bank'), ('土手', 'bank'))
        add_tags(self.d1, 's6', ('大学', 'university'), ('同窓会', 'alumni association'), ('university', 'university'))
        add_tags(self.d1, 's2', ('同窓会', 'association alumni'))

    def merge(self, **kw):
        return self.go('tags-merge', llm=None, **kw)

    def state(self):
        return {sid: tags_of(self.d1, sid) for sid in ('s1', 's2', 's3', 's4', 's5', 's6')}

    def test_dry_run_writes_nothing_prints_clusters_and_candidates(self):
        self.seed()
        before, tags_before = self.snapshot(), self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()
        code, st = self.merge(apply=False)
        self.assertEqual(code, 0, self.text())
        self.assertEqual((self.d1.writes, self.snapshot(), self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()), (0, before, tags_before))
        self.assertGreaterEqual(st['clusters'], 3)
        self.assertGreater(st['rows_to_move'], 0)
        self.assertIn('largest cluster', self.text())
        self.assertIn('動画 / video (x', self.text())
        self.assertIn('synonym candidates', self.text())
        self.assertIn('dry-run', self.text())

    def test_apply_moves_rows_collision_safe_and_is_idempotent(self):
        self.seed()
        code, st = self.merge()
        self.assertEqual(code, 0, self.text())
        s = self.state()
        self.assertEqual(s['s3'], [('動画', 'video')])                                     # video + 動画の|videos on one site: one tag, no PK error
        self.assertEqual(s['s4'], [('動画', 'video'), ('大学', 'university')])             # ビデオ|videos and 大学|universities moved onto the canonicals
        self.assertEqual(s['s5'], [('動画', 'video'), ('土手', 'bank'), ('大学', 'university'), ('銀行', 'bank')])   # 銀行 / 土手 share 'bank': two senses stay two tags;
        # the English-only 'university' on s5 was merged into 大学 (the same site holds it: deleted, no PK error)
        self.assertNotIn(('university', 'university'), s['s5'])
        self.assertEqual(s['s6'], [('同窓会', 'alumni association'), ('大学', 'university')])
        self.assertEqual(s['s2'], [('動画', 'video'), ('同窓会', 'alumni association'), ('犬', 'dog')])      # word order merged
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='university'")[0], 0)
        self.assertEqual((st['verify_mismatch'], st['sites_over_5']), (0, 0))
        self.assertEqual(self.d1.one('SELECT MAX(c) FROM (SELECT COUNT(*) c FROM site_tags GROUP BY site_id)')[0] <= 5, True)
        _c, st2 = self.merge()                                                              # idempotent: nothing left to move
        self.assertEqual((st2['labels_to_move'], st2['updated'], st2['merged']), (0, 0, 0))

    def test_same_label_ja_with_another_label_en_only_rewrites_label_en(self):
        add_site(self.d1, 'a', '山田屋')
        add_site(self.d1, 'b', '山田屋')
        add_tags(self.d1, 'a', ('大学', 'university'))
        add_tags(self.d1, 'a', ('猫', 'cat'))
        add_tags(self.d1, 'b', ('大学', 'universities'))
        add_tags(self.d1, 'b', ('猫', 'cat'))
        _c, st = self.merge()
        self.assertEqual(tags_of(self.d1, 'b'), [('大学', 'university'), ('猫', 'cat')])     # the tag is not deleted by the duplicate rule
        self.assertEqual(st['verify_mismatch'], 0)

    def test_never_more_than_five_and_never_adds_a_tag(self):
        add_site(self.d1, 'x', '山田屋')
        add_tags(self.d1, 'x', ('動画', 'video'), ('ビデオ', 'videos'), ('猫', 'cat'), ('犬', 'dog'), ('鳥', 'bird'))
        self.merge()
        self.assertEqual(tags_of(self.d1, 'x'), [('動画', 'video'), ('犬', 'dog'), ('猫', 'cat'), ('鳥', 'bird')])

    def test_search_text_and_fts_follow_through_the_existing_triggers(self):
        self.seed()
        self.merge()
        for sid in ('s3', 's4', 's6'):
            text = self.d1.one('SELECT search_text FROM sites WHERE id=?', (sid,))[0]
            for ja, en in tags_of(self.d1, sid):
                self.assertIn(en, text)
        self.assertNotIn('universities', self.d1.one('SELECT search_text FROM sites WHERE id=?', ('s4',))[0])
        db = self.d1.db
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('integrity-check')")           # raises when the index disagrees with the content

    def test_limit_and_chunk(self):
        self.seed()
        _c, st = self.merge(limit=1)
        self.assertEqual(st['selected'], 1)
        _c, st = self.merge(tag_chunk=1)
        self.assertEqual(st['verify_mismatch'], 0)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_en='universities'")[0], 0)

    def test_misspelling_merges_only_when_rare_and_the_target_common(self):
        for i in range(8):
            add_site(self.d1, f'u{i}', '山田屋')
            add_tags(self.d1, f'u{i}', ('大学', 'university'))
        add_site(self.d1, 't1', '山田屋')
        add_tags(self.d1, 't1', ('学府', 'universtiy'))
        _c, st = self.merge()
        self.assertEqual(tags_of(self.d1, 't1'), [('大学', 'university')])
        add_site(self.d1, 'm1', '山田屋')                         # two real, equally used words one edit apart: never merged
        add_site(self.d1, 'm2', '山田屋')
        add_tags(self.d1, 'm1', ('管理者', 'manager'))
        add_tags(self.d1, 'm2', ('管理済み', 'managed'))
        self.merge()
        self.assertEqual((tags_of(self.d1, 'm1'), tags_of(self.d1, 'm2')), ([('管理者', 'manager')], [('管理済み', 'managed')]))

    def test_candidate_synonyms_by_cooccurrence_are_reported_not_merged(self):
        for i in range(12):
            sid = f'c{i}'
            add_site(self.d1, sid, '山田屋')
            add_tags(self.d1, sid, ('野球', 'baseball'), ('球団', 'pro team'))
        add_site(self.d1, 'lone', '山田屋')
        add_tags(self.d1, 'lone', ('野球', 'baseball'))
        before = self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()
        _c, st = self.merge()
        self.assertEqual(st['synonym_candidates'], 1)
        self.assertIn('野球 / baseball', self.text())
        self.assertIn('球団 / pro team', self.text())
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall(), before)

    def test_candidates_need_ten_sites_each(self):
        for i in range(9):
            add_site(self.d1, f'c{i}', '山田屋')
            add_tags(self.d1, f'c{i}', ('野球', 'baseball'), ('球団', 'pro team'))
        _c, st = self.merge()
        self.assertEqual(st['synonym_candidates'], 0)

    def test_no_llm_no_files(self):
        import os
        import tempfile
        self.seed()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, _st = self.merge()
                self.assertEqual((code, os.listdir(tmp)), (0, []))
            finally:
                os.chdir(cwd)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM llm_ledger")[0], 0)

    def test_english_label_ja_merges_only_into_an_unambiguous_target(self):
        add_site(self.d1, 'p', '山田屋')
        add_site(self.d1, 'q', '山田屋')
        add_tags(self.d1, 'p', ('bank', 'bank'), ('銀行', 'bank'))
        add_tags(self.d1, 'q', ('土手', 'bank'), ('bank', 'bank'))
        self.merge()
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='bank'")[0], 2)    # 銀行 / 土手 tie: left for the `tags` subcommand


if __name__ == '__main__':
    unittest.main()
