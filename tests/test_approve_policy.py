"""自動承認ポリシー（#129）。ネットワーク不要。"""
import datetime
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import approve_policy as ap
import frontier

TODAY = datetime.date(2026, 11, 18)


def row(**kw):
    base = {'key': 'example.jp', 'title': 'たろうの部屋', 'spam_verdict': 'pass', 'confidence': 'high',
            'description_ja': '個人の趣味サイト。', 'description_en': 'A personal hobby site.', 'categories': json.dumps(['astronomy']),
            'site_kind': 'individual', 'freshness': 'active', 'exempt': 'none', 'freshness_signal_at': '2026-05-01', 'freshness_source': 'header'}
    base.update(kw)
    return base


class Decide(unittest.TestCase):
    def d(self, **kw):
        return ap.explain(row(**kw), TODAY)

    def test_municipal_phrases_are_held(self):
        # 自治体が設置・運営するサイトは、市立などの語が無くても保留にする(2026-11-18 の初回夜間で 2 件すり抜けた)
        for desc in ('都道府県が設置する15〜39歳の若年求職者向け就職支援施設を解説するサイト。',
                     '宮崎県日向市への移住を検討する人向けの、暮らしや支援制度を紹介する移住情報サイト。',
                     '市が運営する子育て情報サイト。'):
            self.assertEqual(self.d(site_kind='organization', description_ja=desc)[0], 'hold', desc)

    def test_ordinary_sites_not_caught_by_municipal_patterns(self):
        for desc in ('静岡県浜松市で葉ねぎの生産と加工を行う農業生産法人のサイト。', '個人の趣味サイト。',
                     '浜松市を中心に給湯器やビルトインコンロの販売・設置、ガス工事を行う地元の工事店のサイト。'):
            self.assertEqual(self.d(description_ja=desc)[0], 'approve', desc)

    def test_active_individual_approved(self):
        self.assertEqual(self.d(), ('approve', 'ok'))
        self.assertEqual(ap.decide(row(), TODAY), 'approve')

    def test_non_individual_ignores_freshness(self):
        for fr in ('stale', 'unknown', None):
            self.assertEqual(self.d(site_kind='business', freshness=fr, freshness_signal_at=None)[0], 'approve')

    def test_stale_individual_without_exemption_rejected(self):
        # 決定論の日付が古く、© 年だけではなく、stale + exempt なし + high のときだけ却下
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2019-01-01'), ('reject_stale', 'stale_individual'))
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2019-01-01', freshness_source='footer_updated')[0], 'reject_stale')
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2019-01-01', freshness_source='feed')[0], 'reject_stale')

    def test_no_signal_is_a_doubt_not_evidence(self):
        self.assertEqual(self.d(freshness='unknown', freshness_signal_at=None, freshness_source=None)[0], 'hold')
        self.assertEqual(self.d(freshness='stale', freshness_signal_at=None, freshness_source=None), ('hold', 'stale_without_signal'))
        self.assertEqual(self.d(freshness='unknown', freshness_signal_at='2019-01-01')[0], 'hold')   # unknown は日付があっても却下しない

    def test_copyright_only_signal_is_weak(self):
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2019-12-31', freshness_source='footer_copyright'), ('hold', 'weak_signal'))

    def test_unknown_site_kind_is_not_taken_as_non_individual(self):
        for kind in ('unknown', None):
            self.assertEqual(self.d(site_kind=kind, freshness='unknown', freshness_signal_at=None)[0], 'hold')
            self.assertEqual(self.d(site_kind=kind, freshness='stale', freshness_signal_at='2019-01-01')[0], 'hold')  # 個人と確定していないので却下しない
            self.assertEqual(self.d(site_kind=kind, freshness=None, freshness_signal_at=None)[0], 'hold')
            self.assertEqual(self.d(site_kind=kind, freshness='active')[0], 'approve')
        for kind in ('business', 'organization', 'company'):
            self.assertEqual(self.d(site_kind=kind, freshness='stale', freshness_signal_at='2019-01-01')[0], 'approve')

    def test_exempt_values(self):
        self.assertEqual(self.d(freshness='stale', exempt='rare_genre')[0], 'hold')
        self.assertEqual(self.d(freshness='unknown', exempt='notable')[0], 'hold')
        self.assertEqual(self.d(freshness='stale', exempt='none', freshness_signal_at=None)[0], 'hold')

    def test_stale_but_unsure_goes_to_human(self):
        self.assertEqual(self.d(freshness='stale', confidence='medium', freshness_signal_at=None)[0], 'hold')

    def test_boundary_exactly_three_years(self):
        # ちょうど 3 年前は stale（以上）。前日は active 側
        self.assertTrue(ap.signal_is_stale('2023-11-18', TODAY))
        self.assertFalse(ap.signal_is_stale('2023-11-19', TODAY))
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2023-11-18')[0], 'reject_stale')
        self.assertEqual(self.d(freshness='stale', freshness_signal_at='2023-11-19')[0], 'hold')       # 信号が新しい: 矛盾は人へ
        self.assertEqual(self.d(freshness='active', freshness_signal_at='2023-11-18')[0], 'hold')     # active と言うが信号は 3 年前
        self.assertEqual(self.d(freshness='active', freshness_signal_at='2023-11-19')[0], 'approve')

    def test_no_signal(self):
        self.assertEqual(self.d(freshness='active', freshness_signal_at=None)[0], 'approve')   # 本文が活動を示す
        self.assertEqual(self.d(freshness=None, freshness_signal_at=None), ('hold', 'freshness_unjudged'))  # #129 以前の判定

    def test_suspect_and_low_hold(self):
        self.assertEqual(self.d(spam_verdict='suspect')[0], 'hold')
        self.assertEqual(self.d(spam_verdict='suspect', confidence='medium')[0], 'hold')
        self.assertEqual(self.d(spam_verdict='deferred', confidence='medium')[0], 'hold')
        self.assertEqual(self.d(confidence='low'), ('hold', 'low_confidence'))
        self.assertEqual(self.d(confidence=None)[0], 'hold')

    def test_medium_confidence_is_approved_like_high(self):
        self.assertEqual(self.d(confidence='medium'), ('approve', 'ok'))
        self.assertEqual(self.d(confidence='medium', site_kind='business', freshness=None, freshness_signal_at=None)[0], 'approve')

    def test_medium_still_needs_other_conditions(self):
        self.assertEqual(self.d(confidence='medium', freshness='unknown', freshness_signal_at=None)[0], 'hold')   # 個人で active でない
        self.assertEqual(self.d(confidence='medium', freshness=None, freshness_signal_at=None)[0], 'hold')
        self.assertEqual(self.d(confidence='medium', site_kind='organization', description_ja='市立図書館のサイト。')[1], 'public_body')
        self.assertEqual(self.d(confidence='medium', description_en=None), ('hold', 'no_description'))
        self.assertEqual(self.d(confidence='medium', categories='[]'), ('hold', 'no_valid_category'))

    def test_medium_is_never_auto_rejected(self):
        # 古い個人サイトの自動却下は high だけ。medium は人の目が要るので保留
        self.assertEqual(self.d(confidence='high', freshness='stale', freshness_signal_at='2019-01-01')[0], 'reject_stale')
        self.assertEqual(self.d(confidence='medium', freshness='stale', freshness_signal_at='2019-01-01'), ('hold', 'stale_individual_unsure'))

    def test_missing_description_or_category(self):
        self.assertEqual(self.d(description_en=None), ('hold', 'no_description'))
        self.assertEqual(self.d(description_ja=''), ('hold', 'no_description'))
        self.assertEqual(self.d(categories='[]'), ('hold', 'no_valid_category'))
        self.assertEqual(self.d(categories=json.dumps(['no-such-category'])), ('hold', 'no_valid_category'))

    def test_public_body_vocabulary(self):
        for word in ('市立', '県立', '広域連合', '消防', '役場', '市役所', '自治体', '教育委員会'):
            self.assertEqual(self.d(site_kind='organization', description_ja=f'{word}のサイト。')[1], 'public_body', word)
        self.assertEqual(self.d(site_kind='organization', title='金沢市役所')[1], 'public_body')
        for host in ('www.city.kanazawa.ishikawa.jp', 'example.lg.jp', 'moj.go.jp', 'school.ed.jp'):
            self.assertEqual(self.d(site_kind='organization', key=host)[1], 'public_body', host)
        self.assertEqual(self.d(site_kind='organization', key='citycafe.jp')[0], 'approve')


class Db(unittest.TestCase):
    def test_dry_run_and_apply_never_touch_human_decisions(self):
        with tempfile.TemporaryDirectory() as td:
            db = frontier.connect(str(Path(td) / 'f.sqlite'))
            def add(key, state='pending', **kw):
                r = row(key=key, **kw)
                db.execute("INSERT INTO frontier (key,host,url,source,discovered_at) VALUES (?,?,?,?,?)", (key, key, 'https://' + key, 'x', 't'))
                db.execute("UPDATE frontier SET title=?, freshness_signal_at=? WHERE key=?", (r['title'], r['freshness_signal_at'], key))
                db.execute("INSERT INTO llm_judgement (key,input_hash,origin,model,judged_at,imported_at,spam_verdict,spam_kind,spam_reason,description_ja,description_en,categories,tags,adult,site_kind,site_kind_reason,site_scope,confidence,review_state,freshness,exempt) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (key, 'h' + key, 'judge', 'm', 't', 't', r['spam_verdict'], 'none', 'r', r['description_ja'], r['description_en'], r['categories'], '[]', 'no',
                            r['site_kind'], 'r', 'standalone', r['confidence'], state, r['freshness'], r['exempt']))
            add('ok.jp')
            add('stale.jp', freshness='stale', freshness_signal_at='2019-01-01')
            add('rare.jp', freshness='stale', exempt='rare_genre', freshness_signal_at='2019-01-01')
            add('human-rejected.jp', state='rejected')
            add('human-approved-stale.jp', state='approved', freshness='stale', freshness_signal_at='2019-01-01')
            db.commit()
            counts, plan = ap.run(db, apply=False, today=TODAY)
            self.assertEqual(sorted((k, d) for k, d, _ in plan), [('ok.jp', 'approve'), ('rare.jp', 'hold'), ('stale.jp', 'reject_stale')])
            self.assertEqual(db.execute("SELECT count(*) FROM llm_judgement WHERE review_state='approved'").fetchone()[0], 1)  # dry-run は書かない
            ap.run(db, apply=True, today=TODAY)
            st = {r['key']: (r['review_state'], r['review_reason']) for r in db.execute('SELECT key, review_state, review_reason FROM llm_judgement')}
            self.assertEqual(st['ok.jp'], ('approved', 'auto_approved'))
            self.assertEqual(st['stale.jp'], ('rejected', 'stale_individual'))
            self.assertEqual(st['rare.jp'], ('pending', None))
            self.assertEqual(st['human-rejected.jp'], ('rejected', None))
            self.assertEqual(st['human-approved-stale.jp'], ('approved', None))

    def test_old_ledger_gets_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / 'old.sqlite')
            c = sqlite3.connect(p)
            old = (ROOT / 'scripts' / 'pipeline' / 'schema.sql').read_text(encoding='utf-8')
            old = '\n'.join(l for l in old.splitlines() if not l.lstrip().startswith(('freshness', 'exempt', 'review_reason')))
            old = re.sub(r',[ \t]*(--[^\n]*)?\n\);', '\n);', old)   # 取り除いた列の直前の末尾カンマ
            c.executescript(old)
            c.commit(); c.close()
            db = frontier.connect(p)
            cols = {r[1] for r in db.execute('PRAGMA table_info(llm_judgement)')}
            self.assertTrue({'freshness', 'exempt', 'review_reason'} <= cols)
            fcols = {r[1] for r in db.execute('PRAGMA table_info(frontier)')}
            self.assertTrue({'freshness_signal_at', 'freshness_source'} <= fcols)


if __name__ == '__main__':
    unittest.main()
