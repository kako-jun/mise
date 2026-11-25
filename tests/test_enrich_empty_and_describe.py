"""Empty-content rejection (judge stage + backfill) and describe-after-approve (deliberate stage).
D1 = in-memory SQLite from the repo's REAL migrations; the LLM is a fake. No network, no production D1, no files."""
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
from enrich import deliberate_stage as ds
from enrich import judge_stage as js
from enrich import publish_stage as ps
import enrich_online as eo
from test_enrich_publish_stage import FakeD1, add_candidate, LEAF

CAT_OF = {'白猫': 'shiro', '黒猫': 'kuro', 'よもぎ猫': 'yomogi'}


class FakeLLM:
    """cats approve (or per-host script), triage -> pass + LEAF, describe -> a description; 'descbad' hosts get garbage / 'descrefuse' an API refusal."""
    def __init__(self, votes=None):
        self.calls, self.votes = [], votes or {}

    def kinds(self):
        return [c['kind'] for c in self.calls]

    def __call__(self, params):
        sysm, text = params['system'][0]['text'], params['messages'][0]['content']
        urls = re.findall(r'url: (\S+)', text)
        kind = next((v for k, v in CAT_OF.items() if f'You are {k},' in sysm), None) or ('triage' if 'STAGE 1' in sysm else 'describe')
        self.calls.append({'kind': kind, 'url': urls[0] if urls else None, 'urls': urls})
        usage = {'input_tokens': 1000, 'output_tokens': 100}
        if kind in CAT_OF.values():
            vote = next((v for k, v in self.votes.items() if k in urls[0]), 'approve')
            out = {'vote': vote, 'reason': '事実に基づく短い理由です。', 'evidence_quote': None}
            return {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}], 'stop_reason': 'end_turn', 'usage': usage}
        if kind == 'describe' and 'descrefuse' in urls[0]:
            raise js.LLMRefused('overloaded')
        if kind == 'describe' and 'descbad' in urls[0]:
            return {'content': [{'type': 'text', 'text': 'not json'}], 'usage': usage}
        items = []
        for n, _u in enumerate(urls, 1):
            if kind == 'triage':
                items.append({'i': n, 'v': 'p', 'kd': '-', 'sk': 'i', 'ad': 'n', 'aq': None, 'sc': 's', 'fr': 'a', 'ex': '-', 'cf': 'h', 'cat': [LEAF], 'r': ''})
            else:
                items.append({'i': n, 'dj': '小さな工房のサイト。', 'de': 'A small workshop site.', 'lj': None, 'le': None, 'ev': [], 'tg': ['craft'],
                              'cc': None, 'rc': None, 'q': None})
        return {'content': [{'type': 'text', 'text': json.dumps({'r': items})}], 'stop_reason': 'end_turn', 'usage': usage}


def add(d1, key, state='candidate', **kw):
    kw.setdefault('review_state', 'pending')
    kw.setdefault('text_head', '小さな工房の紹介ページです。作品を公開しています。')
    kw.setdefault('flags', '{}')
    kw.setdefault('input_hash', 'h-' + key)
    add_candidate(d1, key, state=state, **kw)


def row(d1, key):
    return d1.db.execute('SELECT p.state AS state, c.* FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key '
                         'WHERE p.identity_key=?', (key + '/',)).fetchone()


NODESC = dict(description_ja=None, description_en=None)


class EmptyContent(unittest.TestCase):
    def setUp(self):
        self.d1, self.logs, self.llm = FakeD1(), [], FakeLLM()

    def judge(self, **kw):
        return js.run(self.d1, limit=50, apply=kw.pop('apply', True), log=self.logs.append, llm=self.llm, **kw)

    def fetched(self, key, **kw):
        add(self.d1, key, state='fetched', review_state=None, spam_verdict=None, categories=None, tags=None, **NODESC, **kw)

    def test_judge_rejects_empty_pages_without_llm(self):
        self.fetched('empty.example.jp', text_len=12)
        self.fetched('adult-empty.example.jp', text_len=0, fetch_adult='yes', title=None)
        self.fetched('full.example.jp', text_len=900)
        self.fetched('www.city.example.lg.jp', text_len=10)        # public-body looking: not touched by this rule
        self.fetched('unknown-len.example.jp', text_len=None)
        s = self.judge()
        self.assertEqual(s['empty_rejected'], 2)
        for k in ('empty.example.jp', 'adult-empty.example.jp'):
            r = row(self.d1, k)
            self.assertEqual((r['state'], r['review_state'], r['review_reason']), ('rejected', 'rejected', 'empty_content'))
            self.assertTrue(r['reviewed_at'])
            self.assertTrue(self.d1.one("SELECT ? > strftime('%Y-%m-%dT%H:%M:%SZ','now','+29 days') AND ? < strftime('%Y-%m-%dT%H:%M:%SZ','now','+31 days')",
                                        (r['next_fetch_at'], r['next_fetch_at']))[0])
        urls = {u for c in self.llm.calls for u in c['urls']}
        self.assertFalse({'https://empty.example.jp/', 'https://adult-empty.example.jp/'} & urls)
        self.assertIn('https://full.example.jp/', urls)
        self.assertIn('https://www.city.example.lg.jp/', urls)
        self.assertIn('https://unknown-len.example.jp/', urls)
        self.assertNotEqual(row(self.d1, 'www.city.example.lg.jp')['review_reason'], 'empty_content')

    def test_empty_rejected_even_when_llm_disabled_and_dry_run_writes_nothing(self):
        self.fetched('empty.example.jp', text_len=5)
        w = self.d1.writes
        s = self.judge(apply=False)
        self.assertEqual((s['would_reject_empty'], self.d1.writes), (1, w))
        self.assertEqual(row(self.d1, 'empty.example.jp')['state'], 'fetched')
        s = js.run(self.d1, limit=50, apply=True, log=self.logs.append, llm_enabled=False)
        self.assertEqual(s['empty_rejected'], 1)
        self.assertEqual(self.llm.calls, [])

    def test_row_with_description_is_not_empty(self):
        self.fetched('short.example.jp', text_len=20)
        self.d1.db.execute("UPDATE url_candidates SET description_ja='短いが説明のあるサイト。' WHERE identity_key='short.example.jp/'")
        self.assertEqual(self.judge()['empty_rejected'], 0)

    def pending(self, key, **kw):
        add(self.d1, key, review_reason=kw.pop('review_reason', 'not_pass'), **{**NODESC, **kw})

    def test_backfill_dry_run_apply_idempotent(self):
        self.pending('e1.example.jp', text_len=3)
        self.pending('e2.example.jp', text_len=79, fetch_adult='yes')
        self.pending('e3.example.jp', text_len=0)
        self.pending('www.town.example.jp', text_len=1)                      # public body looking
        self.pending('desc.example.jp', text_len=10, description_ja='説明あり')
        self.pending('boundary.example.jp', text_len=80)
        self.pending('nolen.example.jp', text_len=None)
        add(self.d1, 'done.example.jp', state='rejected', review_state='rejected', text_len=1, **NODESC)
        w = self.d1.writes
        s = js.backfill_empty_content(self.d1, False, log=self.logs.append)
        self.assertEqual((s['empty'], s['rejected'], s['skipped_public_body'], self.d1.writes), (3, 0, 1, w))
        s = js.backfill_empty_content(self.d1, True, log=self.logs.append, page=2)      # paging
        self.assertEqual((s['empty'], s['rejected'], s['lost']), (3, 3, 0))
        for k in ('e1.example.jp', 'e2.example.jp', 'e3.example.jp'):
            r = row(self.d1, k)
            self.assertEqual((r['state'], r['review_state'], r['review_reason']), ('rejected', 'rejected', 'empty_content'))
            self.assertTrue(r['next_fetch_at'])
        for k in ('www.town.example.jp', 'desc.example.jp', 'boundary.example.jp', 'nolen.example.jp'):
            self.assertEqual((row(self.d1, k)['state'], row(self.d1, k)['review_state']), ('candidate', 'pending'))
        s = js.backfill_empty_content(self.d1, True, log=self.logs.append)
        self.assertEqual((s['empty'], s['rejected']), (0, 0))
        self.assertEqual(self.llm.calls, [])

    def test_backfill_skips_row_being_deliberated(self):
        self.pending('busy.example.jp', text_len=1)
        self.d1.db.execute("INSERT INTO deliberations(identity_key,status,input_hash,lease_until) VALUES('busy.example.jp/','running','x',"
                           "strftime('%Y-%m-%dT%H:%M:%SZ','now','+20 minutes'))")
        s = js.backfill_empty_content(self.d1, True, log=self.logs.append)
        self.assertEqual((s['rejected'], s['lost']), (0, 1))
        self.assertEqual(row(self.d1, 'busy.example.jp')['state'], 'candidate')

    def test_cli_backfill_flag_and_reopen_sql(self):
        self.pending('e1.example.jp', text_len=3)
        out = []
        self.assertEqual(eo.main(['--backfill-empty'], client=self.d1, out=out.append), 0)      # dry-run default
        self.assertEqual(row(self.d1, 'e1.example.jp')['state'], 'candidate')
        self.assertEqual(eo.main(['--backfill-empty', '--apply'], client=self.d1, out=out.append), 0)
        self.assertEqual(row(self.d1, 'e1.example.jp')['state'], 'rejected')
        # reopen SQL of docs/operations/enrich.md: not yet due -> nothing; due -> back to 'discovered' with a clean review
        sql = [s for s in REOPEN_SQL]
        for q in sql:
            self.d1.db.execute(q)
        self.assertEqual(row(self.d1, 'e1.example.jp')['state'], 'rejected')
        self.d1.db.execute("UPDATE url_candidates SET next_fetch_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 minutes') WHERE identity_key='e1.example.jp/'")
        for q in sql:
            self.d1.db.execute(q)
        r = row(self.d1, 'e1.example.jp')
        self.assertEqual((r['state'], r['review_state'], r['review_reason']), ('discovered', None, None))


REOPEN_SQL = [
    "UPDATE url_progress SET state='discovered', updated_at=datetime('now') WHERE state='rejected' AND identity_key IN "
    "(SELECT identity_key FROM url_candidates WHERE review_reason='empty_content' AND review_state='rejected' "
    "AND next_fetch_at<=strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
    "UPDATE url_candidates SET review_state=NULL, review_reason=NULL, reviewed_at=NULL, updated_at=datetime('now') "
    "WHERE review_reason='empty_content' AND review_state='rejected' AND identity_key IN (SELECT identity_key FROM url_progress WHERE state='discovered')",
]


class DescribeAfterApprove(unittest.TestCase):
    def setUp(self):
        self.d1, self.logs, self.llm = FakeD1(), [], FakeLLM()

    def go(self, llm=None, **kw):
        return ds.run(self.d1, limit=50, apply=True, log=self.logs.append, llm=llm or self.llm, **kw)

    def test_approve_then_describe_then_published_without_holds(self):
        add(self.d1, 'nodesc.example.jp', review_reason='no_description', text_len=500, **NODESC)
        add(self.d1, 'nocat.example.jp', review_reason='not_pass', text_len=500, categories=json.dumps(['no-such-cat']), tags=None)
        add(self.d1, 'badtags.example.jp', review_reason='not_pass', text_len=500, tags='{oops')
        s = self.go()
        self.assertEqual((s['approved'], s['described'], s['describe_failed']), (3, 3, 0))
        self.assertGreater(s['describe_cost_usd'], 0)
        self.assertGreaterEqual(s['cost_usd'], s['describe_cost_usd'])
        for k in ('nodesc.example.jp', 'nocat.example.jp', 'badtags.example.jp'):
            r = row(self.d1, k)
            self.assertEqual((r['state'], r['review_state'], r['review_reason']), ('approved', 'approved', 'deliberation:approve+described'))
            self.assertEqual(json.loads(r['categories']), [LEAF])
            self.assertEqual(json.loads(r['tags']), ['craft'])
            self.assertTrue(r['description_ja'] and r['description_en'])
        kinds = self.llm.kinds()
        self.assertEqual((kinds.count('describe'), kinds.count('triage')), (3, 1))     # triage only for the row without a usable category
        p = ps.run(self.d1, limit=10, apply=True, log=lambda *_: None)
        self.assertEqual((p['published'], p['held']), (3, 0))

    def test_complete_row_gets_no_describe_call(self):
        add(self.d1, 'ok.example.jp', text_len=500)
        s = self.go()
        self.assertEqual((s['approved'], s['described']), (1, 0))
        self.assertNotIn('describe', self.llm.kinds())
        self.assertEqual(row(self.d1, 'ok.example.jp')['review_reason'], 'deliberation:approve')

    def test_describe_failure_goes_to_meeting(self):
        add(self.d1, 'descbad.example.jp', review_reason='no_description', text_len=500, **NODESC)
        s = self.go()
        self.assertEqual((s['approved'], s['meeting'], s['describe_failed']), (0, 1, 1))
        r = row(self.d1, 'descbad.example.jp')
        self.assertEqual((r['state'], r['review_state'], r['review_reason']), ('candidate', 'pending', 'deliberation:needs_review:gate:no_description'))
        self.assertIsNone(r['description_ja'])
        self.assertEqual(self.d1.one("SELECT status FROM deliberations WHERE identity_key='descbad.example.jp/'")[0], 'meeting')

    def test_api_refusal_during_describe_stops_run_and_row_goes_to_meeting(self):
        add(self.d1, 'descrefuse-a.example.jp', review_reason='no_description', text_len=500, **NODESC)
        add(self.d1, 'z-next.example.jp', review_reason='no_description', text_len=500, **NODESC)
        s = self.go()
        self.assertTrue(s['stopped'] and 'refused' in s['stopped'])
        self.assertEqual(row(self.d1, 'descrefuse-a.example.jp')['review_reason'], 'deliberation:needs_review:gate:no_description')
        self.assertEqual(row(self.d1, 'descrefuse-a.example.jp')['state'], 'candidate')
        self.assertEqual(row(self.d1, 'z-next.example.jp')['state'], 'candidate')          # not voted: the run stopped
        self.assertNotIn(('describe', 'https://z-next.example.jp/'), [(c['kind'], c['url']) for c in self.llm.calls])

    def test_budget_cap_stops_describe(self):
        add(self.d1, 'nodesc.example.jp', review_reason='no_description', text_len=500, **NODESC)
        # the cats fit (3 small reservations), the describe reservation (much bigger) does not
        cap = None
        for cap_try in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
            self.d1.db.execute('DELETE FROM llm_ledger')
            self.d1.db.execute('DELETE FROM deliberations')
            self.d1.db.execute("UPDATE url_progress SET state='candidate'")
            self.d1.db.execute("UPDATE url_candidates SET review_state='pending', review_reason='no_description'")
            llm = FakeLLM()
            s = self.go(llm=llm, budget_month=cap_try)
            if s['describe_failed'] and 'describe' not in llm.kinds() and llm.kinds().count('shiro') == 1:
                cap = cap_try
                break
        self.assertIsNotNone(cap, 'no cap value lets the cats in but not the describe')
        self.assertEqual(row(self.d1, 'nodesc.example.jp')['state'], 'candidate')
        self.assertTrue(s['budget_stopped'])

    def test_verdict_gate_and_non_approve_rows_stay_in_meeting_without_describe(self):
        add(self.d1, 'deferred.example.jp', review_reason='not_pass', spam_verdict='deferred', text_len=500, **NODESC)
        add(self.d1, 'rejvote.example.jp', review_reason='not_pass', text_len=500, **NODESC)
        add(self.d1, 'vetoed.example.jp', review_reason='not_pass', text_len=500, **NODESC)
        llm = FakeLLM({'rejvote': 'hold', 'vetoed': 'hold'})
        s = self.go(llm=llm)
        self.assertEqual((s['described'], s['approved'], s['meeting']), (0, 0, 3))
        self.assertNotIn('describe', llm.kinds())
        self.assertEqual(row(self.d1, 'deferred.example.jp')['review_reason'], 'deliberation:needs_review:gate:verdict_deferred')

    def test_no_files_created(self):
        add(self.d1, 'nodesc.example.jp', review_reason='no_description', text_len=500, **NODESC)
        before = set(os.listdir(tempfile.gettempdir()))
        cwd, tmp = os.getcwd(), tempfile.mkdtemp()
        os.chdir(tmp)
        try:
            self.go()
            js.backfill_empty_content(self.d1, True, log=lambda *_: None)
            self.assertEqual(os.listdir(tmp), [])
        finally:
            os.chdir(cwd)
            os.rmdir(tmp)
        self.assertEqual(before | {os.path.basename(tmp)}, set(os.listdir(tempfile.gettempdir())) | {os.path.basename(tmp)})


if __name__ == '__main__':
    unittest.main()
