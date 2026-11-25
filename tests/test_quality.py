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

    def test_name_acceptable_long(self):
        self.assertTrue(q.name_acceptable_long('あ' * 52))
        self.assertFalse(q.name_acceptable_long('あ' * 81))
        self.assertFalse(q.name_acceptable_long('私たちは地域を支える会社です。'))
        self.assertFalse(q.name_acceptable_long('A｜B'))
        self.assertTrue(q.name_acceptable_long('一般社団法人日本' + 'あ' * 40 + '協会'))
        for t in ('GitHub · Build and ship software on a single, collaborative platform', 'Yamada Tools: the best place to buy tools for you',
                  '私たちは地域の暮らしを支える会社です', '山田製作所は精密部品の製造に取り組んでいます毎日更新中のページ'):
            self.assertFalse(q.name_acceptable_long(t) and len(t) > 40, t)

    def test_is_fragment(self):
        for f in ('山田太郎の', 'Department of', 'Build and ship software on a', 'Foo (Bar'):
            self.assertTrue(q.is_fragment(f), f)
        self.assertFalse(q.is_fragment('Springfield Model Railroad Club'))


def derive(title, host=''):
    d = q.derive_name(title, host)
    return None if d is None else (d['name'], d['name_ja'], d['name_en'])


class DeriveNameTests(unittest.TestCase):
    """The plain rule (owner direction 2026-11-25): split, drop boilerplate segments, drop repeats, keep the rest in order up to NAME_SOFT_MAX.
    Every assertion is the literal result of the rules; where that result is not pretty it is said so."""

    def test_constants(self):
        self.assertEqual((q.NAME_SOFT_MAX, q.NAME_LONG_MAX), (60, 80))

    def test_leading_segments_up_to_sixty_characters(self):
        t = '木更津市の不動産会社｜アクアエステート木更津｜千葉県袖ケ浦・木更津・君津・富津の不動産・住宅情報'
        self.assertEqual(derive(t), (t, None, None))                       # 10 + 1 + 11 + 1 + 26 = 49: all three fit
        longer = t + '｜' + 'あ' * 20                                         # a 4th segment would make it 70 > 60: only the whole segments that fit stay
        self.assertEqual(derive(longer), (t, None, None))
        self.assertEqual(derive('あ' * 30 + '｜' + 'い' * 29 + '｜' + 'う'), ('あ' * 30 + '｜' + 'い' * 29, None, None))   # 60 exactly; the next does not fit

    def test_both_kept_in_order(self):
        t = '石川県白山市にあるアラヤ接骨院 | 肩こり・腰痛・体の歪み・マッサージ'
        self.assertEqual(derive(t), (t, None, None))

    def test_top_label_dropped_company_site_word_stays(self):
        # TOP is a whole boilerplate segment. 「朝倉真弓公式サイト」 is NOT one (the page-kind word is only part of that segment): kept as written.
        self.assertEqual(derive('TOP│朝倉真弓公式サイト｜ウォーキング講師・文筆家・講演家'), ('朝倉真弓公式サイト｜ウォーキング講師・文筆家・講演家', None, None))

    def test_phone_number_removed_and_toppage_dropped(self):
        self.assertEqual(derive('トップページ｜富山県中央植物園ウェブサイト076-466-4187'), ('富山県中央植物園ウェブサイト', None, None))
        self.assertEqual(derive('山田屋｜TEL 03-1234-5678｜〒100-0001 東京都千代田区'), ('山田屋｜東京都千代田区', None, None))
        self.assertEqual(derive('山田屋｜営業時間 9:00-18:00｜https://example.com/'), ('山田屋', None, None))

    def test_official_dealer_site_with_a_brand_is_not_boilerplate(self):
        # 'Official Dealer Site' alone is boilerplate; 'Jeep Official Dealer Site' is a whole segment of its own and stays (literal rule).
        self.assertEqual(derive('ジープ厚木｜Jeep Official Dealer Site'), ('ジープ厚木｜Jeep Official Dealer Site', 'ジープ厚木', 'Jeep Official Dealer Site'))
        self.assertEqual(derive('ジープ厚木｜Official Dealer Site'), ('ジープ厚木', None, None))

    def test_dot_inside_a_name_is_not_a_separator(self):
        t = 'ビーズミル専門メーカー｜乾式も湿式も微粉砕機・分散機なら アシザワ・ファインテック株式会社'
        self.assertEqual(derive(t), (t, None, None))                       # 63 chars would not matter: 22 + 1 + 37 = 60 fits
        self.assertEqual(derive('20th-Century Fox - Studio'), ('20th-Century Fox - Studio', None, None))

    def test_doubled_title_keeps_the_first_segment_only(self):
        t = '100s [Hyaku-Shiki] Official site – 100s.jp100s [Hyaku-Shiki] Official site - 100'
        # 32 + 3 + 37 = 72 > 60: the second segment does not fit, so only the first stays (a page-kind word at the END of a segment is not removed)
        self.assertEqual(derive(t), ('100s [Hyaku-Shiki] Official site', None, None))

    def test_ja_en_pair_sets_both_names(self):
        self.assertEqual(derive('愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY'),
                         ('愛知工業大学 | AICHI INSTITUTE OF TECHNOLOGY', '愛知工業大学', 'AICHI INSTITUTE OF TECHNOLOGY'))
        self.assertEqual(derive('日本抗加齢医学会 | Japanese Society of Anti-aging Medicine'),
                         ('日本抗加齢医学会 | Japanese Society of Anti-aging Medicine', '日本抗加齢医学会', 'Japanese Society of Anti-aging Medicine'))
        self.assertEqual(derive('愛知 | AICHI | 日本'), ('愛知 | AICHI | 日本', '愛知 | 日本', 'AICHI'))      # in order, each joined with its own separator
        self.assertEqual(derive('山田屋 | 2024'), ('山田屋 | 2024', None, None))                          # a digit-only segment is neither language

    def test_one_language_leaves_ja_en_empty(self):
        for t in ('Yahoo! JAPAN', 'ぼくの日記', 'Foo Bar | Baz Inc.'):
            d = q.derive_name(t)
            self.assertEqual((d['name_ja'], d['name_en']), (None, None), t)

    def test_service_name_on_a_shared_host_is_dropped(self):
        t = '2023年度 小児AA/MDS治療研究会学術集会 ハイブリッド開催(現地とWeb併用) | Peatix'
        # the slash always splits (AA / MDS); the name is rejoined with the very separator the title had
        self.assertEqual(derive(t, 'a.peatix.com'), ('2023年度 小児AA/MDS治療研究会学術集会 ハイブリッド開催(現地とWeb併用)', None, None))
        self.assertEqual(derive('山田屋 | Peatix', 'yamada.example.jp'), ('山田屋 | Peatix', '山田屋', 'Peatix'))      # not a shared host: not a service clause

    def test_error_placeholder_is_a_whole_segment_rule(self):
        # 'not found HGU' is NOT the whole placeholder, so by the literal rule it stays (the owner prefers verbose-but-correct to clever-but-wrong)
        self.assertEqual(derive('not found HGU – 北海学園大学受験生向けWEBサイト'),
                         ('not found HGU – 北海学園大学受験生向けWEBサイト', '北海学園大学受験生向けWEBサイト', 'not found HGU'))
        self.assertEqual(derive('404 Not Found | 山田屋'), ('山田屋', None, None))
        self.assertEqual(derive('404 Gallery'), ('404 Gallery', None, None))
        self.assertEqual(derive('Home Sweet Home Bakery'), ('Home Sweet Home Bakery', None, None))
        self.assertEqual(derive('Index of | 山田屋'), ('山田屋', None, None))
        self.assertIsNone(derive('トップページ'))
        self.assertIsNone(derive('Welcome to nginx!'))

    def test_boilerplate_words_between_separators(self):
        self.assertEqual(derive('山田屋｜ホーム'), ('山田屋', None, None))
        self.assertEqual(derive('Home | Yamada Shop'), ('Yamada Shop', None, None))
        self.assertEqual(derive('山田屋 - 公式サイト'), ('山田屋', None, None))
        self.assertEqual(derive('【公式】山田屋｜公式ホームページ'), ('山田屋', None, None))
        self.assertEqual(derive('ようこそ｜山田屋'), ('山田屋', None, None))
        self.assertEqual(derive('山田屋 | ウェブサイト | Official Website'), ('山田屋', None, None))

    def test_repeated_segments_are_dropped_once(self):
        self.assertEqual(derive('山田屋｜山田屋｜ＹＡＭＡＤＡ'), ('山田屋｜ＹＡＭＡＤＡ', '山田屋', 'ＹＡＭＡＤＡ'))
        self.assertEqual(derive('Yamada | YAMADA'), ('Yamada', None, None))

    def test_separator_rules(self):
        self.assertEqual(derive('Foo-Bar Baz'), ('Foo-Bar Baz', None, None))                       # a hyphen inside a word
        self.assertEqual(derive('Foo - Bar'), ('Foo - Bar', None, None))
        self.assertEqual(derive('Adobe: Creative, marketing and document management solutions'),
                         ('Adobe: Creative, marketing and document management solutions', None, None))   # a colon without spaces is no separator: one segment, kept whole
        self.assertEqual(derive('Adobe : Creative'), ('Adobe : Creative', None, None))
        self.assertEqual(derive('神田明神1300年事業 - 文化の鼓動を生む。'), ('神田明神1300年事業 - 文化の鼓動を生む。', None, None))

    def test_never_rewrites_reorders_or_adds(self):
        for t in ('山田屋｜Yamada Shop｜ヤマダ', 'A / B / C', 'x｜' * 3 + 'y'):
            d = q.derive_name(t)
            for seg in d['name'].replace('｜', '|').replace(' / ', '|').split('|'):
                self.assertIn(seg.strip(), t.replace('｜', '|').replace(' / ', '|'))

    def test_first_segment_up_to_eighty_is_kept_whole(self):
        one = '一般社団法人日本' + 'あ' * 60 + '協会'                       # 70 chars
        self.assertEqual(derive(one + '｜お知らせ'), (one, None, None))
        self.assertEqual(derive(one), (one, None, None))

    def test_ninety_character_single_clause(self):
        t = 'あ' * 50 + '、' + 'い' * 39                                     # 90 chars, a 、 inside: cut at the last 、 before 80
        self.assertEqual(derive(t), ('あ' * 50, None, None))
        self.assertIsNone(derive('あ' * 90))                                 # no boundary at all: nothing is guessed (needs_human)
        t2 = 'あ' * 40 + '）' + 'い' * 49
        self.assertEqual(derive(t2), ('あ' * 40 + '）', None, None))
        t3 = 'a ' * 20 + 'b' * 50
        self.assertEqual(derive(t3), ('a ' * 19 + 'a', None, None))

    def test_empty_and_only_boilerplate(self):
        for t in ('', '   ', '公式サイト', 'ホーム | TOP', '|||'):
            self.assertIsNone(derive(t), t)

    def test_suggest_name_is_the_name_of_derive_name(self):
        self.assertEqual(q.suggest_name('ジープ厚木｜Jeep Official Dealer Site', 'x.jp'), 'ジープ厚木｜Jeep Official Dealer Site')
        self.assertIsNone(q.suggest_name('', 'x.jp'))

    def test_deterministic_and_idempotent(self):
        t = 'TOP│朝倉真弓公式サイト｜ウォーキング講師・文筆家・講演家'
        a = q.derive_name(t)
        self.assertEqual(a, q.derive_name(t))
        self.assertEqual(q.derive_name(a['name']), a)                         # deriving from a derived name changes nothing


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
        for d in ('ふくろうの会。', 'ふくろう。', '体の悩みの相談窓口。', '手づくりのくつ。', 'おやつ。', 'ほたる。', 'みんなのうた。', '世界のおやつ。', '夏のほたる。'):
            self.assertEqual(q.check_description_style(d), [], d)
        self.assertEqual(q.check_description_style('体を守る理由だからだ。'), ['description_style'])     # 「〜からだ。」型の文末は常体のまま
        self.assertEqual(q.to_polite('荷物を届ける。'), '荷物を届けます。')
        self.assertEqual(q.to_polite('資料を集める。'), '資料を集めます。')
        self.assertIsNone(q.to_polite('伝統の技を行う。'))          # godan: not converted for sure -> requalify

    def test_hiragana_godan_verbs_are_not_converted(self):
        for core in ('しゃべる', 'あせる', 'かえる', 'こする', '登録がいる', '話をしゃべる'):
            self.assertIn(q._polite_sentence(core), (None, core), core)       # never 'しゃべます' / 'あせます' / 'こします'
        self.assertIsNone(q.to_polite('登録がいる。'))
        self.assertEqual(q.to_polite('そこで暮らしている。'), 'そこで暮らしています。')

    def test_dictionary_and_past_forms_are_plain(self):
        for d in ('伝統の技を行う。', '風景を描く。', '荷物を届ける。', '写真を載せた。', '資料を集めてきた。', '情報を扱う。', '地図を作った。', '歌を歌う。'):
            self.assertEqual(q.check_description_style(d), ['description_style'], d)
        for d in ('ふくろうの会。', 'ふくろう。', '体の悩みの相談窓口。', '手づくりのくつ。', 'おやつ。', 'ほたる。', 'みんなのうた。', '世界のおやつ。', '夏のほたる。'):
            self.assertEqual(q.check_description_style(d), [], d)
        self.assertEqual(q.check_description_style('体を守る理由だからだ。'), ['description_style'])     # 「〜からだ。」型の文末は常体のまま
        self.assertEqual(q.to_polite('荷物を届ける。'), '荷物を届けます。')
        self.assertEqual(q.to_polite('資料を集める。'), '資料を集めます。')
        self.assertIsNone(q.to_polite('伝統の技を行う。'))          # godan: not converted for sure -> requalify

    def test_kanji_stem_conversions(self):
        self.assertEqual(q.to_polite('登録する。'), '登録します。')
        self.assertEqual(q.to_polite('ダウンロードする。'), 'ダウンロードします。')

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
        add('bad', 'x' * 81, '作品を紹介している。', ['reference'], [('video', 'video')])
        res = cdi.quality_checks(lambda sql: con.execute(sql).fetchall())
        self.assertEqual({k: [r[0] for r in v] for k, v in res.items()},
                         {'name_too_long': ['bad'], 'description_style': ['bad'], 'tag_label_ja_not_japanese': ['bad'], 'category_non_leaf_only': ['bad']})
        self.assertEqual(set(cdi.QUALITY_BASELINE), set(res))


if __name__ == '__main__':
    unittest.main()


class SourceValuesTests(unittest.TestCase):
    def test_site_name_must_come_from_values_not_from_labels(self):
        import judge
        rendered = judge.render_candidate({'url': 'https://example.jp/', 'title': 'あおぞら工房', 'meta_description': None, 'lang': 'ja', 'body_excerpt': '木の家具', 'features': {}})
        self.assertEqual(q.check_site_name('あおぞら工房', rendered), [])
        self.assertEqual(q.check_site_name('title', rendered), ['name_not_in_source'])      # the label 'title:' is not the site's text
        self.assertEqual(q.check_site_name('example', rendered), ['name_not_in_source'])    # nor the url
        self.assertEqual(q.check_site_name(None, rendered), [])
