"""scripts/cleanup_quality.py `tags` / `categories` (#273 part 3). Fake D1 = in-memory SQLite built from the repo's REAL migrations (triggers, FTS,
category counters); fake LLM. No network, no production D1, no files."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
sys.path.insert(0, str(ROOT / 'tests'))
import cleanup_quality as cq
import discover_online as do
import enrich_online as eo
import integrity
from enrich import priority
from enrich import quality
from test_cleanup_quality import Base, add_site


class FakeLLM:
    """tags: script[label_ja] = (ja, en) | None. categories: script[site name] = [ids]. Records every call."""
    def __init__(self, tags=None, cats=None):
        self.calls, self.tags, self.cats = [], tags or {}, cats or {}

    def __call__(self, params):
        sysm, text = params['system'][0]['text'], params['messages'][0]['content']
        items = json.loads(re.search(r'<source>\n(.*)\n</source>', text, re.S).group(1))
        self.calls.append((sysm[:20], items))
        if 'tag labels' in sysm:
            out = {'r': [{'i': it['i'], 'ja': (self.tags.get(it['ja']) or (None, it['en']))[0], 'en': (self.tags.get(it['ja']) or (None, it['en']))[1]}
                         for it in items]}
        else:
            out = {'r': [{'i': it['i'], 'ids': self.cats.get(it['name'], [])} for it in items]}
        return {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 900, 'output_tokens': 80}}


def add_tags(d1, sid, *tags):
    for ja, en in tags:
        d1.db.execute('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)', (sid, ja, en))


def tags_of(d1, sid):
    return sorted(tuple(r) for r in d1.db.execute('SELECT label_ja,label_en FROM site_tags WHERE site_id=?', (sid,)))


class TagsBase(Base):
    def seed(self):
        for sid in ('s1', 's2', 's3', 's4', 's5', 's6'):
            add_site(self.d1, sid, '山田屋')
        add_tags(self.d1, 's1', ('鳥', 'bird'), ('bird', 'bird'))          # a Japanese label for the same English label exists
        add_tags(self.d1, 's2', ('鳥', 'bird'))
        add_tags(self.d1, 's3', ('鳥', 'bird'), ('video', 'video'), ('動画', 'video'))     # video next to 動画: a duplicate to merge
        add_tags(self.d1, 's4', ('videos', 'videos'), ('Linux', 'linux'))                         # synonym table: videos -> 動画; Linux is an allowlisted proper noun
        add_tags(self.d1, 's5', ('bird', 'bird'), ('birdwatching', 'birdwatching'))
        add_tags(self.d1, 's6', ('birdwatching', 'birdwatching'))

    def foreign(self):
        return self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja GLOB '[A-Za-z]*'")[0]


class Tags(TagsBase):
    def test_dry_run_writes_nothing_and_calls_no_llm_and_estimates_cost(self):
        self.seed()
        before, llm = self.snapshot(), FakeLLM()
        tags_before = self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()
        code, st = self.go('tags', apply=False, llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual((llm.calls, self.d1.writes), ([], 0))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall(), tags_before)
        self.assertEqual((st['by_table'], st['by_majority'], st['would_llm']), (2, 1, 1))    # video, videos / bird / birdwatching
        self.assertGreater(st['est_cost_usd_max'], 0)
        self.assertEqual(st['foreign_labels'], 4)
        self.assertIn('before -> after', self.text())

    def test_apply_table_majority_llm_merge_and_allowlist(self):
        self.seed()
        llm = FakeLLM(tags={'birdwatching': ('バードウォッチング', 'birdwatching')})
        code, st = self.go('tags', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(tags_of(self.d1, 's1'), [('鳥', 'bird')])                          # the English duplicate merged away, no PK violation
        self.assertEqual(tags_of(self.d1, 's3'), [('動画', 'video'), ('鳥', 'bird')])          # video merged into the existing 動画
        self.assertEqual(tags_of(self.d1, 's4'), [('Linux', 'linux'), ('動画', 'video')])             # table; the proper noun is untouched
        self.assertEqual(tags_of(self.d1, 's5'), [('バードウォッチング', 'birdwatching'), ('鳥', 'bird')])
        self.assertEqual(tags_of(self.d1, 's6'), [('バードウォッチング', 'birdwatching')])
        self.assertEqual((st['by_table'], st['by_majority'], st['by_llm'], st['llm_calls']), (2, 1, 1, 1))   # ONE call for every undecided label
        self.assertEqual(st['merged'], 2)
        self.assertEqual((st['foreign_labels_after'], st['foreign_rows_after']), (0, 0))
        self.assertEqual(self.foreign(), 1)                                                        # only the allowlisted Linux is left
        self.assertEqual((st['verify_mismatch'], st['needs_human']), (0, 0))
        _c, st2 = self.go('tags', llm=llm)                                                         # idempotent: fixed labels stop matching
        self.assertEqual((st2['selected'], st2['updated'], len(llm.calls)), (0, 0, 1))

    def test_llm_answers_are_validated(self):
        add_site(self.d1, 'v', '山田屋')
        bad = {'sentence': ('これは鳥についての文章です', 'sentence'), 'junk': ('鳥/学部', 'junk'), 'long': ('あ' * 21, 'long'),
               'latin': ('College', 'latin'), 'changed': ('果物', 'banana'), 'upper': ('果物', 'Fruit'), 'ja_en': ('果物', 'くだもの'),
               'admin': ('市', 'admin'), 'none': (None, 'none'), 'good': ('果物', 'fruit')}
        for k in bad:
            add_tags(self.d1, 'v', (k, k))
        # 'good' => 果物 / fruit does not keep the English label (good != fruit): rejected too; make a true match
        self.d1.db.execute("UPDATE site_tags SET label_ja='fruit', label_en='fruit' WHERE label_ja='good'")
        bad['fruit'] = ('果物', 'fruit')
        del bad['good']
        # label 'upper' answers 'Fruit' (not lower case) and 'changed' answers banana for another label: both rejected
        code, st = self.go('tags', llm=FakeLLM(tags=bad))
        self.assertEqual(code, 0, self.text())
        self.assertEqual(tags_of(self.d1, 'v').count(('果物', 'fruit')), 1)
        self.assertEqual(st['by_llm'], 1)
        self.assertEqual(st['needs_human'], 9)
        self.assertEqual(len(tags_of(self.d1, 'v')), 10)                                           # rejected labels stay as they were
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip' AND cursor_key LIKE 'tags:%'")[0], 9)

    def test_needs_human_is_skipped_until_retry(self):
        add_site(self.d1, 'v', '山田屋')
        add_tags(self.d1, 'v', ('zzqx', 'zzqx'))
        llm = FakeLLM()
        self.go('tags', llm=llm)
        self.assertEqual(len(llm.calls), 1)
        _c, st = self.go('tags', llm=llm)
        self.assertEqual((len(llm.calls), st['skip_listed'], st['selected']), (1, 1, 0))
        llm2 = FakeLLM(tags={'zzqx': ('ズズ', 'zzqx')})
        _c, st = self.go('tags', llm=llm2, retry_skipped=True)
        self.assertEqual((st['by_llm'], tags_of(self.d1, 'v')), (1, [('ズズ', 'zzqx')]))
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip'")[0], 0)

    def test_ties_go_to_the_llm_not_to_a_guess(self):
        add_site(self.d1, 'v', '山田屋')
        add_site(self.d1, 'w', '山田屋')
        add_tags(self.d1, 'v', ('音', 'sound'), ('sound', 'sound'))
        add_tags(self.d1, 'w', ('サウンド', 'sound'))
        llm = FakeLLM()
        _c, st = self.go('tags', apply=False, llm=llm)
        self.assertEqual((st['by_majority'], st['would_llm']), (0, 1))

    def test_never_more_than_five_tags_and_pk_never_violated(self):
        add_site(self.d1, 'v', '山田屋')
        add_tags(self.d1, 'v', ('動画', 'video'), ('video', 'video'), ('videos', 'videos'), ('music', 'music'))
        code, st = self.go('tags', llm=FakeLLM(tags={}))
        self.assertEqual(code, 0, self.text())
        got = tags_of(self.d1, 'v')
        self.assertLessEqual(len(got), 5)
        self.assertEqual(sorted(got), [('動画', 'video'), ('音楽', 'music')])      # video/videos merged into 動画 
        self.assertEqual(quality.check_tags([{'label_ja': a, 'label_en': b} for a, b in got]), [])  # no duplicate / English left

    def test_equivalent_japanese_label_is_not_added_twice(self):
        add_site(self.d1, 'v', '山田屋')
        add_tags(self.d1, 'v', ('ビデオ', 'video'), ('video', 'video'))                            # ビデオ is a variant of 動画 in the synonym table
        self.go('tags')
        self.assertEqual(tags_of(self.d1, 'v'), [('ビデオ', 'video')])

    def test_search_text_and_fts_follow_tag_changes(self):
        self.seed()
        db = self.d1.db
        db.execute("UPDATE sites SET search_text=lower(name) WHERE 1")
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('rebuild')")
        db.execute("UPDATE fts_state SET ready=1 WHERE name='sites_fts'")
        self.go('tags', llm=FakeLLM(tags={'birdwatching': ('バードウォッチング', 'birdwatching')}))
        text = self.d1.one("SELECT search_text FROM sites WHERE id='s5'")[0]
        self.assertIn('バードウォッチング birdwatching', text)
        self.assertIn('鳥 bird', text)
        self.assertNotIn('bird bird', text)
        self.assertNotIn('動画 video video', self.d1.one("SELECT search_text FROM sites WHERE id='s3'")[0])
        db.execute("INSERT INTO sites_fts(sites_fts) VALUES('integrity-check')")                   # raises when the index disagrees with the content
        hit = db.execute("SELECT s.id FROM sites_fts f JOIN sites s ON s.rowid=f.rowid WHERE sites_fts MATCH '\"バードウォッチング\"'").fetchall()
        self.assertEqual(sorted(r[0] for r in hit), ['s5', 's6'])

    def test_large_label_is_moved_in_chunks_and_read_back(self):
        n = cq.TAG_ROW_CHUNK * 2 + 17
        for i in range(n):
            add_site(self.d1, f'm{i}', '山田屋')
            add_tags(self.d1, f'm{i}', ('movie', 'movie'))
        add_site(self.d1, 'ref', '山田屋')
        add_tags(self.d1, 'ref', ('映画', 'movie'))
        code, st = self.go('tags')
        self.assertEqual((code, st['updated'], st['verify_mismatch']), (0, n, 0), self.text())
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='映画'")[0], n + 1)
        self.assertLessEqual(self.d1.max_params, 100)

    def test_limit_counts_distinct_labels(self):
        self.seed()
        _c, st = self.go('tags', llm=FakeLLM(), limit=2, apply=False)
        self.assertEqual(st['selected'], 2)

    def test_budget_stop_leaves_deterministic_results_and_the_rest(self):
        self.seed()
        code, st = self.go('tags', llm=FakeLLM(), budget_month=0.0000001)
        self.assertEqual(code, 0, self.text())
        self.assertIsNotNone(st['stopped'])
        self.assertEqual(tags_of(self.d1, 's6'), [('birdwatching', 'birdwatching')])
        self.assertEqual(tags_of(self.d1, 's2'), [('鳥', 'bird')])

    def test_lease_bookmark_and_no_files(self):
        self.seed()
        other = do.Lease(self.d1, key='cleanup', owner='someone-else')
        self.assertTrue(other.acquire())
        before = self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()
        code, _ = self.go('tags')
        self.assertEqual(code, 2)
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall(), before)
        other.release()
        eo.time_travel_bookmark = lambda c: (_ for _ in ()).throw(RuntimeError('no token'))
        llm = FakeLLM()
        code, _ = self.go('tags', llm=llm)
        self.assertEqual((code, llm.calls), (1, []))
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall(), before)
        eo.time_travel_bookmark = lambda c: 'bm-test'
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.go('tags')
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(cwd)
        self.assertIn('bm-test', self.text())

    def test_main_default_is_dry_run(self):
        self.seed()
        before = self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall()
        self.assertEqual(cq.main(['tags'], client=self.d1), 0)
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_tags ORDER BY 1,2').fetchall(), before)


# ------------------------------------------------------------------------------------------------------------- categories
def tree_of(d1):
    return cq.load_tree(d1)


def cats_of(d1, sid):
    return sorted(r[0] for r in d1.db.execute('SELECT category_id FROM site_categories WHERE site_id=?', (sid,)))


def add_cats(d1, sid, *ids):
    for c in ids:
        d1.db.execute('INSERT INTO site_categories (site_id,category_id) VALUES (?,?)', (sid, c))


def counters(d1):
    return (d1.db.execute('SELECT category_id,scope,n FROM category_counts WHERE n<>0 ORDER BY 1,2').fetchall(),
            d1.db.execute('SELECT scope,n FROM scope_counts ORDER BY 1').fetchall())


class Categories(Base):
    def setUp(self):
        super().setUp()
        self.tree = tree_of(self.d1)
        self.leaf_edu = sorted(self.tree.below('education'))
        self.assertIn('universities', self.leaf_edu)

    def seed(self):
        add_site(self.d1, 'u', '山田大学', desc='山田大学の公式サイト。', country_code='JP')
        add_cats(self.d1, 'u', 'education')
        add_site(self.d1, 'two', '山田研究所', desc='物理と化学の研究所。', country_code='US')
        add_cats(self.d1, 'two', 'science', 'education')
        add_site(self.d1, 'has_leaf', '佐藤学校')
        add_cats(self.d1, 'has_leaf', 'education', 'schools')                   # already holds a leaf: not selected
        add_site(self.d1, 'gov', '山田市', desc='山田市役所。')
        add_cats(self.d1, 'gov', 'government')                                  # only assign=rule leaves below: no candidate
        add_site(self.d1, 'none', '無分類')

    def run_apply(self, llm, **kw):
        return self.go('categories', llm=llm, **kw)

    def test_dry_run_writes_nothing_and_no_llm(self):
        self.seed()
        before, llm = self.snapshot(), FakeLLM()
        cats = self.d1.db.execute('SELECT * FROM site_categories ORDER BY 1,2').fetchall()
        code, st = self.go('categories', apply=False, llm=llm)
        self.assertEqual((code, llm.calls, self.d1.writes), (0, [], 0), self.text())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.d1.db.execute('SELECT * FROM site_categories ORDER BY 1,2').fetchall(), cats)
        self.assertEqual((st['selected'], st['would_llm'], st['needs_human']), (3, 2, 1))  # u, two + gov (no candidate: needs_human, never sent)
        self.assertGreater(st['est_cost_usd_max'], 0)

    def test_apply_replaces_non_leaf_with_chosen_leaf_and_keeps_counters_consistent(self):
        self.seed()
        errs_before = integrity.errors_of(integrity.run_checks(integrity.sqlite_execute(self.d1.db)))
        llm = FakeLLM(cats={'山田大学': ['universities'], '山田研究所': ['physics', 'chemistry']})
        code, st = self.run_apply(llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(cats_of(self.d1, 'u'), ['universities'])
        self.assertEqual(cats_of(self.d1, 'two'), ['chemistry', 'education', 'physics'])   # the unrelated non-leaf row stays; science is replaced
        self.assertEqual(cats_of(self.d1, 'has_leaf'), ['education', 'schools'])           # untouched
        self.assertEqual(cats_of(self.d1, 'gov'), ['government'])
        self.assertEqual((st['updated'], st['verify_mismatch'], st['by_llm'], st['llm_calls']), (2, 0, 2, 1))   # ONE call for the batch of 10
        self.assertEqual(st['needs_human_ids'], ['gov'])
        errs_after = integrity.errors_of(integrity.run_checks(integrity.sqlite_execute(self.d1.db)))
        self.assertEqual(errs_after, errs_before)                                                     # category_counts / scope_counts / closure unchanged
        self.assertEqual(self.d1.db.execute('SELECT n FROM category_counts WHERE category_id=? AND scope=?', ('education', 'all')).fetchone()[0], 3)
        self.assertEqual(self.d1.db.execute('SELECT n FROM category_counts WHERE category_id=? AND scope=?', ('science', 'all')).fetchone()[0], 1)
        self.assertEqual(self.d1.db.execute('SELECT n FROM category_counts WHERE category_id=? AND scope=?', ('universities', 'JP')).fetchone()[0], 1)
        self.assertEqual(self.d1.db.execute("SELECT COUNT(*) FROM site_categories a JOIN category_closure cl ON cl.ancestor_id=a.category_id "
                                            "AND cl.descendant_id<>a.category_id JOIN site_categories b ON b.site_id=a.site_id "
                                            "AND b.category_id=cl.descendant_id").fetchone()[0], 1)       # only has_leaf (education + schools) was like this before
        _c, st2 = self.run_apply(llm, restart=True)                                                   # idempotent
        self.assertEqual((st2['selected'], st2['updated'], len(llm.calls)), (0, 0, 1))

    def test_answer_outside_the_candidates_is_rejected(self):
        self.seed()
        outside = ['hospitals',          # a leaf, but under another top-level category
                   'education',          # not a leaf
                   'cities']             # an assign=rule leaf
        for bad in outside:
            before = cats_of(self.d1, 'u')
            llm = FakeLLM(cats={'山田大学': [bad], '山田研究所': ['universities', bad]})
            _c, st = self.run_apply(llm, restart=True, retry_skipped=True)
            self.assertEqual(cats_of(self.d1, 'u'), before, bad)
            self.assertEqual(cats_of(self.d1, 'two'), ['education', 'science'], bad)
            self.assertEqual(st['by_llm'], 0)
        self.assertIn('cities', priority.rule_leaf_ids())
        llm = FakeLLM(cats={'山田大学': ['no-such-category'], '山田研究所': []})                      # invented id / none
        _c, st = self.run_apply(llm, restart=True, retry_skipped=True)
        self.assertEqual((cats_of(self.d1, 'u'), cats_of(self.d1, 'two')), (['education'], ['education', 'science']))
        self.assertEqual(sorted(st['needs_human_ids']), ['gov', 'two', 'u'])
        llm = FakeLLM(cats={'山田大学': ['universities', 'schools', 'cram-schools', 'lessons']})     # more than 3
        self.run_apply(llm, restart=True, retry_skipped=True)
        self.assertEqual(cats_of(self.d1, 'u'), ['education'])

    def test_candidates_shown_to_the_llm_are_only_choosable_leaves_below_the_sites_own_categories(self):
        self.seed()
        llm = FakeLLM()
        self.run_apply(llm)
        items = {it['name']: it for _s, its in llm.calls for it in its}
        ids = {c['id'] for c in items['山田大学']['c']}
        self.assertEqual(ids, set(self.leaf_edu))
        self.assertFalse(ids & priority.rule_leaf_ids())
        self.assertTrue(ids <= self.tree.leaves)
        self.assertEqual({c['id'] for c in items['山田研究所']['c']}, set(self.tree.below('science')) | set(self.leaf_edu))
        self.assertNotIn('山田市', items)                                                             # no candidate: never sent

    def test_total_never_exceeds_three_and_ancestor_rows_above_the_chosen_leaf_go(self):
        add_site(self.d1, 'm', '山田', desc='x。')
        add_cats(self.d1, 'm', 'education', 'science', 'society')
        _c, st = self.run_apply(FakeLLM(cats={'山田': ['universities', 'physics', 'law']}))
        got = cats_of(self.d1, 'm')
        self.assertLessEqual(len(got), 3)
        self.assertEqual(got, ['law', 'physics', 'universities'])                                     # all three non-leaf rows replaced one for one
        add_site(self.d1, 'n', '山田2', desc='x。')
        add_cats(self.d1, 'n', 'education', 'science', 'society')
        self.run_apply(FakeLLM(cats={'山田2': ['universities', 'schools', 'physics']}), restart=True)
        got = cats_of(self.d1, 'n')
        self.assertLessEqual(len(got), 3)
        self.assertTrue(any(c in self.tree.leaves for c in got))

    def test_mid_level_category_gets_leaves_below_it(self):
        mids = [c for c in self.tree.parent if self.tree.parent[c] and c not in self.tree.leaves and self.tree.below(c)]
        if not mids:
            self.skipTest('the category tree has no mid-level node')
        mid = mids[0]
        add_site(self.d1, 'mid', '中間', desc='x。')
        add_cats(self.d1, 'mid', mid)
        leaf = sorted(self.tree.below(mid))[0]
        self.run_apply(FakeLLM(cats={'中間': [leaf]}))
        self.assertEqual(cats_of(self.d1, 'mid'), [leaf])

    def test_guard_when_the_site_changed_meanwhile(self):
        self.seed()
        self.d1.before_batch = lambda db: db.execute("DELETE FROM site_categories WHERE site_id='u'")
        _c, st = self.run_apply(FakeLLM(cats={'山田大学': ['universities']}))
        self.assertEqual(cats_of(self.d1, 'u'), [])                                                    # the guard inserted nothing (the old row was gone)

    def test_skip_marks_cursor_and_retry(self):
        self.seed()
        llm = FakeLLM()
        _c, st = self.run_apply(llm)
        self.assertEqual(sorted(st['needs_human_ids']), ['gov', 'two', 'u'])
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip' AND cursor_key LIKE 'categories:%'")[0], 3)
        _c, st = self.run_apply(llm, restart=True)
        self.assertEqual((st['selected'], st['skip_listed']), (0, 3))
        _c, st = self.run_apply(FakeLLM(cats={'山田大学': ['universities']}), restart=True, retry_skipped=True)
        self.assertEqual(cats_of(self.d1, 'u'), ['universities'])
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM discovery_cursors WHERE source='cleanup-skip' AND cursor_key='categories:u'")[0], 0)

    def test_limit_and_cursor_resume(self):
        for n in range(5):
            add_site(self.d1, f'p{n}', f'サイト{n}', desc='x。')
            add_cats(self.d1, f'p{n}', 'education')
        llm = FakeLLM(cats={f'サイト{n}': ['universities'] for n in range(5)})
        _c, st = self.run_apply(llm, limit=2)
        self.assertEqual(st['updated'], 2)
        _c, st = self.run_apply(llm, limit=10)
        self.assertEqual(st['updated'], 3)

    def test_lease_bookmark_budget_and_no_files(self):
        self.seed()
        other = do.Lease(self.d1, key='cleanup', owner='someone-else')
        self.assertTrue(other.acquire())
        code, _ = self.run_apply(FakeLLM())
        self.assertEqual(code, 2)
        other.release()
        eo.time_travel_bookmark = lambda c: (_ for _ in ()).throw(RuntimeError('no token'))
        llm = FakeLLM(cats={'山田大学': ['universities']})
        code, _ = self.run_apply(llm)
        self.assertEqual((code, llm.calls, cats_of(self.d1, 'u')), (1, [], ['education']))
        eo.time_travel_bookmark = lambda c: 'bm-test'
        code, st = self.run_apply(llm, budget_month=0.0000001)
        self.assertEqual((code, cats_of(self.d1, 'u')), (0, ['education']))
        self.assertIsNotNone(st['stopped'])
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.run_apply(llm, restart=True)
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(cwd)
        self.assertEqual(cats_of(self.d1, 'u'), ['universities'])


if __name__ == '__main__':
    unittest.main()
