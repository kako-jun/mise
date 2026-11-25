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
    def __init__(self, tags=None, cats=None, same=None, drop_verify=(), whys=None, tag_nulls=()):
        self.calls, self.tags, self.cats = [], tags or {}, cats or {}
        self.models, self.whys, self.tag_nulls = [], whys or {}, set(tag_nulls)
        self.bad_verify = 0                                              # the next N verification calls answer with text that is no JSON
        self.same, self.drop_verify = same or {}, set(drop_verify)        # verification answers by the Japanese label (default: yes)

    def __call__(self, params):
        sysm, text = params['system'][0]['text'], params['messages'][0]['content']
        items = json.loads(re.search(r'<source>\n(.*)\n</source>', text, re.S).group(1))
        self.calls.append((sysm[:20], items))
        self.models.append(params['model'])
        if 'translations of English tag labels' in sysm and self.bad_verify > 0:
            self.bad_verify -= 1
            return {'content': [{'type': 'text', 'text': 'not json'}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 900, 'output_tokens': 80}}
        if 'translations of English tag labels' in sysm:
            out = {'r': [{'i': it['i'], 'same': self.same.get(it['ja'], True)} for it in items if it['ja'] not in self.drop_verify]}
        elif 'tag labels' in sysm:
            out = {'r': [{'i': it['i'], 'ja': (self.tags.get(it['ja']) or (None, it['en']))[0], 'en': (self.tags.get(it['ja']) or (None, it['en']))[1]}
                         for it in items]}
        else:
            out = {'r': [{'i': it['i'], 'ids': self.cats.get(it['name'], []), 'why': self.whys.get(it['name'], it['name'][:2])} for it in items]}
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
        self.assertEqual((st2['selected'], st2['updated'], len(llm.calls)), (0, 0, 2))

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
        self.assertEqual(cq.TAG_ROW_CHUNK, 50)
        n = cq.TAG_ROW_CHUNK * 2 + 17
        for i in range(n):
            add_site(self.d1, f'm{i}', '山田屋')
            add_tags(self.d1, f'm{i}', ('movie', 'movie'))
        for r in ('ref1', 'ref2'):
            add_site(self.d1, r, '山田屋')
            add_tags(self.d1, r, ('映画', 'movie'))
        code, st = self.go('tags')
        self.assertEqual((code, st['updated'], st['verify_mismatch']), (0, n, 0), self.text())
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='映画'")[0], n + 2)
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

    # ---- review S1 / S2: meaning checks and the majority floor
    def one_site(self, *tags):
        add_site(self.d1, 'v', '山田屋')
        add_tags(self.d1, 'v', *tags)

    def test_reviewed_wrong_translations_are_rejected_by_the_second_call(self):
        self.one_site(('bank', 'bank'), ('data science', 'data science'), ('lyon', 'lyon'), ('bird watching', 'bird watching'))
        llm = FakeLLM(tags={'bank': ('土手', 'bank'), 'data science': ('機械学習', 'data science'), 'lyon': ('リヨン市の観光', 'lyon'),
                            'bird watching': ('バードウォッチング', 'bird watching')},
                      same={'土手': False, '機械学習': False, 'リヨン市の観光': False})
        code, st = self.go('tags', llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(tags_of(self.d1, 'v'), sorted([('bank', 'bank'), ('data science', 'data science'), ('lyon', 'lyon'),
                                                       ('バードウォッチング', 'bird watching')]))
        self.assertEqual((st['by_llm'], st['verify_rejected'], st['verify_calls'], st['llm_calls']), (1, 3, 1, 1))
        self.assertEqual(st['needs_human'], 3)
        verify = [c for c in llm.calls if c[0].startswith('You check')]
        self.assertEqual(len(verify), 1)                                                            # ONE verification call for the batch
        self.assertEqual({it['ja'] for it in verify[0][1]}, {'土手', '機械学習', 'リヨン市の観光', 'バードウォッチング'})

    def test_missing_or_unclear_verification_means_no_change(self):
        self.one_site(('bank', 'bank'), ('lyon', 'lyon'))
        llm = FakeLLM(tags={'bank': ('銀行', 'bank'), 'lyon': ('リヨン', 'lyon')}, drop_verify=['銀行'])
        _c, st = self.go('tags', llm=llm)
        self.assertIn(('bank', 'bank'), tags_of(self.d1, 'v'))                                      # no answer = not applied
        self.assertIn(('リヨン', 'lyon'), tags_of(self.d1, 'v'))
        self.assertEqual(st['verify_rejected'], 1)

    def test_nothing_is_verified_or_written_in_dry_run_but_the_cost_covers_both_calls(self):
        self.one_site(('bank', 'bank'))
        llm = FakeLLM()
        _c, st = self.go('tags', apply=False, llm=llm)
        self.assertEqual(llm.calls, [])
        solo = cq.judge.worst_case(cq.CHEAP_MODEL, cq.TAG_SYSTEM + json.dumps(cq.TAG_SCHEMA), cq.tag_user([(1, {'ja': 'bank', 'en': 'bank'})]),
                                   cq.tag_max_tokens(1))[0]
        self.assertGreater(st['est_cost_usd_max'], solo)

    def big(self, n, label=('bank', 'bank')):
        for i in range(n):
            add_site(self.d1, f'b{i}', '山田屋')
            add_tags(self.d1, f'b{i}', label)

    def test_large_llm_mapping_needs_the_flag(self):
        self.big(cq.LARGE_LLM_ROWS + 1)
        llm = FakeLLM(tags={'bank': ('銀行', 'bank')})
        _c, st = self.go('tags', llm=llm)
        self.assertEqual((st['updated'], st['large_llm_mapping'], st['needs_human']), (0, 1, 1))
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='bank'")[0], cq.LARGE_LLM_ROWS + 1)
        self.assertIn('large LLM mapping NOT applied', self.text())
        self.assertEqual(self.d1.one("SELECT cursor_value FROM discovery_cursors WHERE cursor_key='tags:bank'")[0][:18], 'large_llm_mapping ')
        _c, st = self.go('tags', llm=llm, retry_skipped=True, allow_large_llm_mapping=True)
        self.assertEqual((st['updated'], st['large_llm_mapping']), (cq.LARGE_LLM_ROWS + 1, 0))
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM site_tags WHERE label_ja='銀行'")[0], cq.LARGE_LLM_ROWS + 1)

    def test_exactly_the_threshold_is_applied_without_the_flag(self):
        self.big(cq.LARGE_LLM_ROWS)
        _c, st = self.go('tags', llm=FakeLLM(tags={'bank': ('銀行', 'bank')}))
        self.assertEqual(st['updated'], cq.LARGE_LLM_ROWS)

    def test_large_flag_still_needs_the_verification(self):
        self.big(cq.LARGE_LLM_ROWS + 1)
        _c, st = self.go('tags', llm=FakeLLM(tags={'bank': ('土手', 'bank')}, same={'土手': False}), allow_large_llm_mapping=True)
        self.assertEqual((st['updated'], st['verify_rejected']), (0, 1))

    def test_one_row_cannot_decide_a_majority(self):
        add_site(self.d1, 'one', '山田屋')
        add_tags(self.d1, 'one', ('土手', 'bank'))                                                   # a single supporting row
        self.big(30, ('bank', 'bank'))
        _c, st = self.go('tags', apply=False, llm=FakeLLM())
        self.assertEqual((st['by_majority'], st['would_llm']), (0, 1))

    def test_majority_needs_sixty_percent_and_a_strict_lead(self):
        for i, ja in enumerate(['銀行'] * 5 + ['土手'] * 3 + ['堤'] * 2):                             # 50% / 30% / 20%: ahead but under 60%
            add_site(self.d1, f'm{i}', '山田屋')
            add_tags(self.d1, f'm{i}', (ja, 'bank'))
        add_site(self.d1, 'f', '山田屋')
        add_tags(self.d1, 'f', ('bank', 'bank'))
        _c, st = self.go('tags', apply=False)
        self.assertEqual((st['by_majority'], st['would_llm']), (0, 1))
        for i in range(10, 20):
            add_site(self.d1, f'm{i}', '山田屋')
            add_tags(self.d1, f'm{i}', ('銀行', 'bank'))                                              # 15 / 20 = 75%
        _c, st = self.go('tags', apply=False)
        self.assertEqual(st['by_majority'], 1)
        self.assertEqual(cq.MAJORITY_MIN_ROWS, 2)

    def test_chunk_option_is_bounded(self):
        with self.assertRaises(SystemExit):
            cq.main(['tags', '--chunk', '101'], client=self.d1)
        with self.assertRaises(SystemExit):
            cq.main(['names', '--allow-large-llm-mapping'], client=self.d1)
        self.assertEqual(cq.main(['tags', '--chunk', '100'], client=self.d1), 0)

    # ---- LLM path on real-looking labels (production run: 290 labels, all needs_human)
    REAL = {'3d printing': ('3Dプリント', '3d printing'), '24-hour service': ('24時間対応', '24-hour service'), 'academic conference': ('学術大会', 'academic conference'),
            'abashiri': ('網走', 'abashiri'), 'aa': ('AA', 'aa'), '3d cad': ('3DCAD', '3d cad'), 'accessibility': ('アクセシビリティ', 'accessibility'),
            'action figures': ('アクションフィギュア', 'action figure'), 'adventure games': ('アドベンチャーゲーム', 'adventure game'),
            'amateur radio': ('アマチュア無線', 'amateur radio'), 'bird watching': ('バードウォッチング', 'bird watching'), 'craft beer': ('クラフトビール', 'craft beer')}

    def test_real_labels_are_adopted_on_the_llm_path(self):
        self.one_site(*[(k, k) for k in self.REAL][:5])
        for i, k in enumerate(list(self.REAL)[5:]):
            add_site(self.d1, f'x{i}', '山田屋')
            add_tags(self.d1, f'x{i}', (k, k))
        llm = FakeLLM(tags=self.REAL)
        code, st = self.go('tags', llm=llm)
        self.assertEqual((code, st['by_llm'], st['needs_human'], st['verify_calls']), (0, len(self.REAL), 0, 1), self.text())
        got = {r[0] for r in self.d1.db.execute('SELECT label_ja FROM site_tags')}
        self.assertEqual(got, {v[0] for v in self.REAL.values()})
        self.assertEqual(self.d1.one("SELECT label_en FROM site_tags WHERE label_ja='アクションフィギュア'")[0], 'action figure')   # singular
        self.assertEqual(set(llm.models), {cq.CHEAP_MODEL})                                           # tags stay on the cheap model
        self.assertEqual(sorted(x for x in got if not quality.has_japanese(x)), ['3DCAD', 'AA'])            # Latin tokens that need no translation (digit / capitals)

    def test_latin_tokens_need_no_translation_and_are_not_selected(self):
        self.one_site(*[(k, k) for k in ('5g', '4k', '3d', '.net', 'a24', '12-step', '1920s', 'c++', 'FPGA')], ('university', 'university'))
        llm = FakeLLM()
        _c, st = self.go('tags', apply=False, llm=llm)
        self.assertEqual((st['latin_ok_labels'], st['foreign_labels'], st['selected']), (9, 1, 1))
        for t in ('5G|5g', '.NET|.net', 'A24|a24', '12-step|12-step', '1920s|1920s', '3D|3d', 'FPGA|fpga'):
            self.assertEqual(quality.check_tags([t]), [], t)
        for t in ('university|university', 'Music|music', 'open-source|open-source', 'web components|web components', 'aa|aa', 'nostr|nostr'):
            self.assertEqual(quality.check_tags([t]), ['tag_label_ja_not_japanese'], t)

    def test_answers_for_digit_and_abbreviation_labels(self):
        syn, pr = quality.default_synonyms(), quality.proper_nouns()
        ok = [('12-step', '12ステップ'), ('1920s', '1920年代'), ('5g', '5G'), ('4k', '4K'), ('.net', '.NET'), ('a24', 'A24'), ('aa', 'AA'),
              ('3d printing', '3Dプリント'), ('24-hour service', '24時間対応'), ('abashiri', '網走')]
        for en, ja in ok:
            self.assertEqual(cq.check_tag_answer({'ja': en, 'en': en, 'n': 1}, ja, en, syn, pr), ((ja, en), None), (en, ja))
        bad = [('nostr', 'Nostr', 'nostr', 'not_japanese'), ('university', 'University', 'university', 'not_japanese'),
               ('bank', 'bank', 'bank', 'same_as_old'), ('3d printing', None, '3d printing', 'llm_null'),
               ('3d printing', '3Dプリントとは何ですか。', '3d printing', 'sentence_like'), ('3d printing', '3Dプリント', '3D printing', 'label_en_invalid'),
               ('3d printing', '3Dプリント', 'printing', 'label_en_changed'), ('3d printing', '3D/プリント', '3d printing', 'junk_chars'),
               ('3d printing', 'あ' * 21, '3d printing', 'too_long'), ('3d printing', '市', '3d printing', 'admin_only')]
        for en, ja, new_en, why in bad:
            self.assertEqual(cq.check_tag_answer({'ja': en, 'en': en, 'n': 1}, ja, new_en, syn, pr), (None, why), (en, ja))
        self.assertEqual(cq.check_tag_answer({'ja': 'x', 'en': 'x', 'n': 1}, '果物', cq.KEY_MISSING, syn, pr), (None, 'llm_missing_key'))

    def test_reason_codes_are_reported_and_explain_lists_every_label(self):
        self.one_site(('zzcode', 'zzcode'), ('nostr', 'nostr'), ('bank', 'bank'))
        llm = FakeLLM(tags={'nostr': ('Nostr', 'nostr'), 'bank': ('土手', 'bank')}, same={'土手': False})
        _c, st = self.go('tags', llm=llm)
        reasons = dict(st['needs_human_reasons'])
        self.assertEqual(reasons, {'zzcode': 'llm_null', 'nostr': 'not_japanese', 'bank': 'not_same_meaning'})
        self.assertIn('needs_human by reason:', self.text())
        self.assertIn('llm_null: 1', self.text())
        self.assertNotIn('\n    zzcode', self.text())                                                # the default shows counts and a few examples only
        self.out.clear()
        _c, st = self.go('tags', llm=llm, retry_skipped=True, explain=True)
        self.assertIn('\n    zzcode', self.text())

    def test_explain_in_main(self):
        self.assertEqual(cq.main(['tags', '--explain'], client=self.d1), 0)

    def test_verification_is_retried_once_and_a_failure_is_a_reason(self):
        self.one_site(('bank', 'bank'))
        llm = FakeLLM(tags={'bank': ('銀行', 'bank')})
        llm.bad_verify = 1
        _c, st = self.go('tags', llm=llm)
        self.assertEqual((st['by_llm'], st['verify_calls']), (1, 1))                                  # the second attempt answered
        add_site(self.d1, 'w', '山田屋')
        add_tags(self.d1, 'w', ('banker', 'banker'))
        llm = FakeLLM(tags={'banker': ('銀行員', 'banker')})
        llm.bad_verify = 2
        _c, st = self.go('tags', llm=llm)
        self.assertEqual(dict(st['needs_human_reasons']), {'banker': 'verify_no_answer'})
        self.assertGreater(cq.verify_max_tokens(100), 4000)                                           # room for 100 answers

    def test_prompt_tells_the_model_not_to_null_short_or_digit_labels(self):
        self.assertIn('never null just because', cq.TAG_SYSTEM)
        self.assertNotIn('an abbreviation you are unsure of', cq.TAG_SYSTEM)


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

    # ---- model and evidence
    def test_categories_default_to_sonnet_and_estimate_with_it(self):
        self.seed()
        self.assertEqual(cq.CAT_MODEL, cq.judge.MODELS['sonnet'])
        _c, st = self.go('categories', apply=False)
        self.assertEqual(st['model'], cq.CAT_MODEL)
        _c, st_h = self.go('categories', apply=False, model='haiku')
        self.assertEqual(st_h['model'], cq.CHEAP_MODEL)
        self.assertGreater(st['est_cost_usd_max'], st_h['est_cost_usd_max'] * 5)                      # sonnet is priced well above haiku
        llm = FakeLLM(cats={'山田大学': ['universities']})
        self.run_apply(llm)
        self.assertEqual(set(llm.models), {cq.CAT_MODEL})
        llm2 = FakeLLM(cats={})
        self.run_apply(llm2, model='haiku', restart=True, retry_skipped=True)
        self.assertEqual(set(llm2.models), {cq.CHEAP_MODEL})
        self.assertEqual(cq.main(['categories', '--model', 'haiku'], client=self.d1), 0)
        with self.assertRaises(SystemExit):
            cq.main(['names', '--model', 'haiku'], client=self.d1)

    def test_hesitation_is_an_empty_answer_and_the_categories_stay(self):
        self.seed()
        llm = FakeLLM(cats={})                                                                        # empty ids everywhere
        _c, st = self.run_apply(llm)
        self.assertEqual((cats_of(self.d1, 'u'), cats_of(self.d1, 'two')), (['education'], ['education', 'science']))
        reasons = dict(st['needs_human_reasons'])
        self.assertEqual((reasons['u'], reasons['two'], reasons['gov']), ('llm_empty', 'llm_empty', 'no_candidates'))
        self.assertEqual(st['updated'], 0)

    def test_evidence_must_be_words_of_the_name_or_description(self):
        self.seed()
        llm = FakeLLM(cats={'山田大学': ['universities'], '山田研究所': ['physics']}, whys={'山田大学': '神社|寺', '山田研究所': ''})
        _c, st = self.run_apply(llm)
        self.assertEqual((cats_of(self.d1, 'u'), cats_of(self.d1, 'two')), (['education'], ['education', 'science']))
        reasons = dict(st['needs_human_reasons'])
        self.assertEqual((reasons['u'], reasons['two']), ('evidence_not_in_text', 'no_evidence'))
        llm = FakeLLM(cats={'山田大学': ['universities']}, whys={'山田大学': '山田|公式サイト'})                  # every word is in the name / description
        self.run_apply(llm, restart=True, retry_skipped=True)
        self.assertEqual(cats_of(self.d1, 'u'), ['universities'])

    def test_evidence_check_function(self):
        site = {'name': 'Internet Sacred Text Archive', 'd': '世界の宗教の聖典を集めた電子図書館。', 'cand_ids': {'religion-x', 'shrines'}}
        self.assertEqual(cq.cat_validate(site, ['religion-x'], '聖典|宗教'), (['religion-x'], None))
        self.assertEqual(cq.cat_validate(site, ['religion-x'], 'sacred text'), (['religion-x'], None))   # case / spaces ignored
        self.assertEqual(cq.cat_validate(site, ['shrines'], '神社')[1], 'evidence_not_in_text')
        self.assertEqual(cq.cat_validate(site, ['shrines'], '聖|宗教')[1], 'evidence_not_in_text')       # one-character pieces do not count
        self.assertEqual(cq.cat_validate(site, ['temples'], '聖典')[1], 'outside_candidates')
        self.assertEqual(cq.cat_validate(site, [], '聖典')[1], 'llm_empty')
        self.assertEqual(cq.cat_validate(site, ['religion-x'], None)[1], 'no_evidence')

    def test_prompt_demands_clear_fit_and_evidence(self):
        for w in ('CLEARLY', 'EMPTY', 'why', 'subject', 'VERBATIM'):
            self.assertIn(w, cq.CAT_SYSTEM)

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
