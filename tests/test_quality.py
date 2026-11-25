"""scripts/enrich/quality.py: the rules for the public-bound fields (Issue #273). Pure functions; no network, no D1."""
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
import quality as q  # noqa: E402
import judge  # noqa: E402

INFIGO = 'Infigo online｜新感覚アートブランドが提案する、美しい発色のグラフィックアイテム｜インフィゴのオンラインショップ'
JLPA = '一般社団法人日本エルピーガスプラント協会（JLPA）/LPガス・LNGの自主保安、保安の確保と防災のための活動'
UBUGOE = 'うぶごえナビ｜産声が響く場所を、探そう。沖縄鹿児島…'
SYN = q.load_synonyms(str(ROOT / 'data' / 'tag-synonyms.csv'))


class NameTests(unittest.TestCase):
    def test_good_names(self):
        for n in ('Infigo online', '通助', 'ことほむ｜時代考証', 'AC/DC Fan Club', 'Yahoo! JAPAN', '日本エルピーガスプラント協会（JLPA）', 'a' * 40,
                  'Foo | Home', '木工房 たろう - 作品集'):
            self.assertEqual(q.check_name(n), [], n)

    def test_empty_and_too_long(self):
        self.assertEqual(q.check_name(''), ['name_empty'])
        self.assertEqual(q.check_name('   '), ['name_empty'])
        self.assertEqual(q.check_name(None), ['name_empty'])
        self.assertIn('name_too_long', q.check_name('a' * 41))
        self.assertEqual(q.NAME_MAX, 40)

    def test_lists_and_slogans(self):
        for t in (INFIGO, JLPA, UBUGOE, 'A｜B｜C', '通助｜地域活性化・地域ブランド・マーケティング支援の会社', 'Foo Co. - Tokyo - Osaka'):
            self.assertIn('name_list', q.check_name(t), t)

    def test_two_clauses_short_subtitle_ok_but_long_second_is_not(self):
        self.assertEqual(q.check_name('たろう工房｜公式'), [])
        self.assertIn('name_list', q.check_name('たろう工房｜手作り家具と雑貨を作っています毎日更新中でお待ちしています'))

    def test_promo_is_only_a_signal(self):
        self.assertIn('name_promo', q.check_name('革小物 公式通販｜Leather Shop'))
        self.assertEqual(q.check_name('公式通販'), [])                    # a short name that merely contains the word
        self.assertIn('name_promo', q.check_name('家具をご提案するインテリアショップ たなか'))

    def test_sentence(self):
        self.assertIn('name_sentence', q.check_name('私たちは地域の暮らしを支える会社です。'))

    def test_suggest_real_looking_titles(self):
        self.assertEqual(q.suggest_name(INFIGO, 'infigo.jp'), 'Infigo online')
        self.assertEqual(q.suggest_name(JLPA, 'jlpa.or.jp'), '一般社団法人日本エルピーガスプラント協会（JLPA）')   # the form stays when the whole name fits
        self.assertEqual(q.suggest_name(UBUGOE, 'ubugoe-navi.jp'), 'うぶごえナビ')
        self.assertEqual(q.suggest_name('ことほむ｜時代考証・コンテンツプロデュース – 歴史に学び、未来へ', 'kotohomu.com'), None)   # 3 clauses, no sign: no guess

    def test_suggest_prefers_the_host_brand_and_strips_site_type(self):
        self.assertEqual(q.suggest_name('新作情報のお知らせ｜Mochi Bakery 公式サイト｜焼きたてを毎朝お届け', 'mochi-bakery.jp'), 'Mochi Bakery')
        self.assertEqual(q.suggest_name('たろう工房 公式ホームページ', 'taro.jp'), 'たろう工房')
        self.assertEqual(q.suggest_name('Foo Bar - Official Site', 'foo.com'), 'Foo Bar')

    def test_corporate_form_is_kept_when_it_fits_and_dropped_only_to_fit(self):
        self.assertEqual(q.suggest_name('株式会社ミライ｜採用情報｜お問い合わせ・会社概要・アクセス・プライバシー', 'mirai.co.jp'), '株式会社ミライ')
        long_corp = '一般社団法人日本' + 'あいうえお' * 6 + '協会'      # 6+2+30+2 = 40 -> fits; add one char to force the drop
        self.assertEqual(q.suggest_name(long_corp + '｜採用情報｜お知らせと案内ページ一覧', 'x.jp'), long_corp)
        too_long = '一般社団法人' + long_corp
        self.assertEqual(q.suggest_name(too_long + '｜採用情報｜お知らせと案内ページ一覧', 'x.jp'), long_corp)      # 46 chars: the form is dropped to fit
        self.assertIsNone(q.suggest_name('株式会社｜採用情報｜お問い合わせ', 'x.jp'))      # a corporate form alone is not a name

    def test_several_clauses_without_a_sign_give_none_not_the_first(self):
        self.assertEqual(q.suggest_name('金属加工 | 精密部品 | 株式会社山田製作所', 'yamada.co.jp'), '株式会社山田製作所')    # the one clause with a corporate form
        self.assertIsNone(q.suggest_name('ニュース - 天気 - スポーツ - 北陸新聞', 'hokuriku.jp'))
        self.assertIsNone(q.suggest_name('株式会社 | 山田製作所 | 金属加工', 'y.jp'))
        self.assertEqual(q.suggest_name('Mochi Bakery｜パン｜ケーキ｜クッキー', 'mochi-bakery.jp'), 'Mochi Bakery')   # the host's brand

    def test_long_single_clause_is_kept_by_the_publish_rule(self):
        name52 = 'あ' * 52
        self.assertTrue(q.name_acceptable_long(name52))
        self.assertFalse(q.name_acceptable_long('あ' * 81))
        self.assertFalse(q.name_acceptable_long('私たちは地域を支える会社です。'))
        self.assertFalse(q.name_acceptable_long('A｜B'))

    def test_fragments_are_never_returned(self):
        self.assertIsNone(q.suggest_name('山田太郎のホームページ | 鉄道模型 | 写真', 't.jp'))
        for t in ('GitHub · Build and ship software on a single, collaborative platform', 'Homepage of the Springfield Model Railroad Club Association',
                  'Yamada Laboratory, Department of Mechanical Engineering and Robotics Systems'):
            self.assertIsNone(q.suggest_name(t, 'x.example'), t)
        for f in ('山田太郎の', 'Department of', 'Build and ship software on a', 'Foo (Bar'):
            self.assertTrue(q.is_fragment(f), f)
        self.assertFalse(q.is_fragment('Springfield Model Railroad Club'))
        self.assertEqual(q.suggest_name('Acme Robotics (Research Division of the Long Named Institute for Sciences)', 'acme.example'), 'Acme Robotics')

    def test_suggest_never_cuts_inside_a_clause(self):
        self.assertIsNone(q.suggest_name('あ' * 60, 'x.jp'))
        self.assertIsNone(q.suggest_name('', 'x.jp'))
        self.assertIsNone(q.suggest_name('公式サイト', 'x.jp'))

    def test_suggest_result_always_passes_check_name(self):
        for t in (INFIGO, JLPA, UBUGOE, 'Foo', 'a b c d e f g h i j k l m n o p q r s t u v w x y z a b c d e f g'):
            r = q.suggest_name(t, 'x.jp')
            self.assertTrue(r is None or q.check_name(r) == [], (t, r))


class DescriptionTests(unittest.TestCase):
    def test_ok(self):
        for d in ('小さな工房の紹介サイト。', '地域の歴史を紹介しています。', '作品を公開していました。', '旅行記録', '食品の製造販売。', '猫の写真を載せています', 'English only text is out of scope.'):
            self.assertEqual(q.check_description_style(d), [], d)

    def test_plain_forms_rejected(self):
        for d in ('作品を紹介している。', 'これは辞書である。', '日本の伝統工芸を紹介する。', '地域で活動を行った。', '情報はまだ少ない。', '週末に開かれる。',
                  '美術館の案内です。作品を展示している。', '店主が書いたブログだ。', '写真を撮っている'):
            self.assertEqual(q.check_description_style(d), ['description_style'], d)

    def test_to_polite(self):
        cases = {'作品を紹介している。': '作品を紹介しています。', 'これは辞書である。': 'これは辞書です。', '手作り雑貨を販売する。': '手作り雑貨を販売します。',
                 '地域を紹介した。': '地域を紹介しました。', '店主の趣味のブログだ。': '店主の趣味のブログです。', '週末に開かれる。': '週末に開かれます。',
                 '展示を行っていた。': '展示を行っていました。', '建物は古くからある。': '建物は古くからあります。',
                 '工房の紹介サイト。作品を公開している。': '工房の紹介サイト。作品を公開しています。', '写真を載せている': '写真を載せています'}
        for a, b in cases.items():
            self.assertEqual(q.to_polite(a), b, a)
            self.assertEqual(q.check_description_style(b), [])

    def test_bare_iru_is_not_converted(self):
        self.assertIsNone(q.to_polite('登録がいる。'))
        self.assertEqual(q.to_polite('そこで暮らしている。'), 'そこで暮らしています。')

    def test_dictionary_and_past_forms_are_plain(self):
        for d in ('伝統の技を行う。', '風景を描く。', '荷物を届ける。', '写真を載せた。', '資料を集めてきた。', '情報を扱う。', '地図を作った。', '歌を歌う。'):
            self.assertEqual(q.check_description_style(d), ['description_style'], d)
        for d in ('ふくろうの会。', 'ふくろう。', 'からだ。', '体の悩みの相談窓口。', '手づくりのくつ。'):
            self.assertEqual(q.check_description_style(d), [], d)
        self.assertEqual(q.to_polite('荷物を届ける。'), '荷物を届けます。')
        self.assertEqual(q.to_polite('資料を集める。'), '資料を集めます。')
        self.assertIsNone(q.to_polite('伝統の技を行う。'))          # godan: not converted for sure -> requalify

    def test_to_polite_is_conservative(self):
        for d in ('地域で活動を行った。', '本を読んだ。', '情報はまだ少ない。', '作品を作った。'):
            self.assertIsNone(q.to_polite(d), d)
        self.assertEqual(q.to_polite('もう丁寧です。'), 'もう丁寧です。')
        self.assertEqual(q.to_polite('体言止め。'), '体言止め。')
        self.assertIsNone(q.to_polite(None))


class TagTests(unittest.TestCase):
    def test_good(self):
        self.assertEqual(q.check_tags(['動画|video', '写真|photography'], SYN), [])
        self.assertEqual(q.check_tags([{'label_ja': '猫', 'label_en': 'cat'}], SYN), [])
        self.assertEqual(q.check_tags([], SYN), [])

    def test_english_label_ja_is_rejected(self):
        self.assertEqual(q.check_tags(['university'], SYN), ['tag_label_ja_not_japanese'])
        self.assertIn('tag_label_ja_not_japanese', q.check_tags(['university|university']))

    def test_en_label_must_be_english(self):
        self.assertIn('tag_label_en_not_english', q.check_tags(['動画|動画']))
        self.assertIn('tag_label_en_not_english', q.check_tags(['動画|']))

    def test_near_duplicates(self):
        self.assertIn('tag_duplicate', q.check_tags(['動画|video', '映像|videos'], SYN))
        self.assertIn('tag_duplicate', q.check_tags(['猫|cat', 'ねこ|cats'], SYN))
        self.assertIn('tag_duplicate', q.check_tags(['ゲーム|game', 'ＧＡＭＥ|Games'], SYN))
        self.assertEqual(q.check_tags(['動画|video', '音楽|music'], SYN), [])

    def test_count_length_malformed(self):
        self.assertIn('tag_count', q.check_tags([f'タグ{i}|tag{i}' for i in range(6)]))
        self.assertIn('tag_too_long', q.check_tags(['あ' * 21 + '|x']))
        self.assertIn('tag_malformed', q.check_tags([5]))
        self.assertEqual(q.check_tags('x'), ['tag_malformed'])

    def test_proper_nouns_and_halfwidth_kana_and_admin_words(self):
        self.assertEqual(q.check_tags(['FPGA|fpga', 'iOS|ios', 'Linux|linux']), [])          # listed Latin-script proper nouns may stand as label_ja
        self.assertEqual(q.check_tags(['Nonexistentthing|thing']), ['tag_label_ja_not_japanese'])
        self.assertEqual(q.check_tags(['ｱﾆﾒ|anime']), [])                                      # half-width kana are Japanese (NFKC)
        self.assertIn('tag_admin_only', q.check_tags(['市|city']))
        self.assertEqual(q.check_tags(['市場調査|market research']), [])

    def test_proper_noun_file_is_valid(self):
        pn = q.load_proper_nouns(str(ROOT / 'data' / 'tag-proper-nouns.csv'))
        self.assertTrue({'fpga', 'ios', 'linux'} <= pn)

    def test_normalize_tag_key(self):
        n = q.normalize_tag_key
        self.assertEqual(n('Videos'), n('video'))
        self.assertEqual(n('Hip-Hop'), n('hip hop'))
        self.assertEqual(n('ＡＢＣ'), n('abc'))
        self.assertEqual(n('Libraries'), n('library'))
        self.assertEqual(n('movies'), n('movie'))
        self.assertEqual(n('Cookies'), n('cookie'))
        self.assertEqual(n('Boxes'), n('box'))
        self.assertEqual(n('glass'), 'glass')
        self.assertEqual(n('動画'), '動画')
        self.assertEqual(n('  '), '')

    def test_synonyms(self):
        self.assertEqual(SYN.key('映像'), SYN.key('videos'))
        self.assertNotEqual(SYN.key('Movie'), SYN.key('動画'))     # a movie is not 'video': only unambiguous synonyms are in the table
        self.assertNotEqual(SYN.key('designer'), SYN.key('デザイン'))
        self.assertEqual(SYN.canonical('Videos'), ('動画', 'video'))
        self.assertIsNone(SYN.canonical('zzz'))

    def test_synonym_file_is_valid(self):
        self.assertGreaterEqual(len(SYN.rows), 30)
        for ja, en, variants in SYN.rows:        # load_synonyms already raised on any invalid row; assert the shape again
            self.assertTrue(q.has_japanese(ja) and en and not q.has_japanese(en))
        self.assertEqual(q.check_tags([f'{ja}|{en}' for ja, en, _v in SYN.rows[:5]], SYN), [])

    def test_synonym_loader_rejects_bad_rows(self):
        import tempfile
        for body, frag in (('canonical_ja,canonical_en,variants\nvideo,video,videos\n', 'no Japanese'),
                           ('canonical_ja,canonical_en,variants\n動画,動画,x\n', 'non-Japanese'),
                           ('canonical_ja,canonical_en,variants\n動画,video,movie\n映画,film,movies|movie\n', 'already belongs')):
            with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8') as f:
                f.write(body)
            with self.assertRaises(ValueError) as cm:
                q.load_synonyms(f.name)
            self.assertIn(frag, str(cm.exception))


class CategoryTests(unittest.TestCase):
    def setUp(self):
        tree = judge.load_tree()[0]
        self.leaf = q.leaf_ids_of(list(tree.items()))

    def test_leaf_only(self):
        self.assertIn('libraries', self.leaf)
        self.assertNotIn('reference', self.leaf)
        self.assertEqual(q.check_categories(['libraries'], self.leaf), [])
        self.assertEqual(q.check_categories(['libraries', 'reference'], self.leaf), ['category_non_leaf'])
        self.assertEqual(q.check_categories(['reference'], self.leaf), ['category_non_leaf'])
        self.assertEqual(q.check_categories(['no-such'], self.leaf), ['category_non_leaf'])
        self.assertEqual(q.check_categories([], self.leaf), ['category_none'])
        self.assertEqual(q.check_categories(None, self.leaf), ['category_none'])

    def test_which(self):
        self.assertEqual([q.which(c) for c in ('name_list', 'description_style', 'tag_duplicate', 'category_none')],
                         ['name', 'description', 'tags', 'categories'])


class IntegrityReportTests(unittest.TestCase):
    def test_quality_checks_count_the_violations(self):
        spec = importlib.util.spec_from_file_location('cdi', ROOT / 'scripts' / 'check-data-integrity.py')
        cdi = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cdi)
        import export_sites
        db = export_sites.load_db(str(ROOT))
        con = sqlite3.connect(':memory:')
        db.backup(con)
        cols = [r[1] for r in con.execute('PRAGMA table_info(sites)')]
        for t in ('site_tags', 'site_categories', 'sites'):
            con.execute(f'DELETE FROM {t}')
        def add(i, name, desc, cats, tags):
            con.execute('INSERT INTO sites (id,url,name,description_ja,description_en) VALUES (?,?,?,?,?)', (i, f'https://{i}.example/', name, desc, 'd'))
            for c in cats:
                con.execute('INSERT INTO site_categories VALUES (?,?)', (i, c))
            for t in tags:
                con.execute('INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)', (i, *t))
        add('good', 'Good', '小さな工房の紹介サイト。', ['libraries'], [('動画', 'video')])
        add('bad', 'x' * 41, '作品を紹介している。', ['reference'], [('video', 'video')])
        res = cdi.quality_checks(lambda sql: con.execute(sql).fetchall())
        self.assertEqual({k: [r[0] for r in v] for k, v in res.items()},
                         {'name_too_long': ['bad'], 'description_style': ['bad'], 'tag_label_ja_not_japanese': ['bad'], 'category_non_leaf_only': ['bad']})
        self.assertEqual(set(cdi.QUALITY_BASELINE), set(res))


if __name__ == '__main__':
    unittest.main()
