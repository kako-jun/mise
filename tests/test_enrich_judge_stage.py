"""Stage B judge (scripts/enrich/judge_stage.py). Fake D1 (in-memory SQLite) + fake LLM. Never calls the real API or Cloudflare."""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
import judge_stage as js  # noqa: E402
import judge  # noqa: E402

CAT = judge.load_categories()[1]['id']
KEY_ENV = 'ANTHROPIC_API_KEY'


class FakeD1:
    """client.query / client.batch over SQLite; enforces the 100 bound-parameter limit; records every statement."""

    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.log = []
        self.fail_settle = False
        for f in ('0036_url_candidates.sql', '0040_url_candidates_site_name.sql', '0038_llm_ledger.sql'):
            self.db.executescript((ROOT / 'migrations' / f).read_text(encoding='utf-8'))
        self.db.execute("CREATE TABLE url_progress (identity_key TEXT PRIMARY KEY, url TEXT NOT NULL, state TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")

    def _one(self, sql, params):
        assert len(params) <= 100, 'more than 100 bound parameters'
        self.log.append(sql)
        if self.fail_settle and sql.startswith("UPDATE llm_ledger SET kind='call'"):
            self.fail_settle = False
            raise RuntimeError('d1 down')
        cur = self.db.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []

    def query(self, sql, params=()):
        with self.lock:
            r = self._one(sql, list(params))
            self.db.commit()
            return r

    def batch(self, statements):
        with self.lock:
            try:
                out = [self._one(s, list(p)) for s, p in statements]
                self.db.commit()
                return out
            except Exception:
                self.db.rollback()
                raise

    def add(self, key, state='fetched', **cols):
        base = {'identity_key': key, 'url': 'https://' + key, 'title': f'{key} の部屋', 'description': 'about', 'lang': 'ja',
                'text_head': '小さな工房の紹介ページです。作品を公開しています。', 'flags': '{}'}
        base.update(cols)
        with self.lock:
            self.db.execute('INSERT INTO url_progress (identity_key,url,state) VALUES (?,?,?)', (key, 'https://' + key, state))
            self.db.execute(f"INSERT INTO url_candidates ({','.join(base)}) VALUES ({','.join('?' * len(base))})", list(base.values()))
            self.db.commit()

    def state(self, key):
        return self.query('SELECT state FROM url_progress WHERE identity_key=?', [key])[0]['state']

    def cand(self, key):
        return self.query('SELECT * FROM url_candidates WHERE identity_key=?', [key])[0]

    def writes(self):
        return [s for s in self.log if not s.lstrip().upper().startswith('SELECT')]


class FakeLLM:
    """Answers triage / describe requests. 'spam' -> reject, 'thin' -> low confidence, 'bad' -> garbage, 'refuse' -> LLMRefused."""

    def __init__(self, tokens=(1000, 200)):
        self.calls = []
        self.tokens = tokens
        self.lock = threading.Lock()

    def __call__(self, params):
        with self.lock:
            self.calls.append(params)
        text = params['messages'][0]['content']
        stage = 'triage' if 'STAGE 1' in params['system'][0]['text'] else 'describe'
        urls = re.findall(r'url: (\S+)', text)
        if any('refuse' in u for u in urls):
            raise js.LLMRefused('overloaded')
        items = []
        for n, u in enumerate(urls, 1):
            if 'bad' in u:
                return {'content': [{'type': 'text', 'text': 'not json'}], 'usage': {'input_tokens': 10, 'output_tokens': 5}}
            if stage == 'triage':
                v, kd, cf = ('r', 'spam', 'h') if 'spam' in u else ('p', '-', 'l' if 'thin' in u else 'h')
                items.append({'i': n, 'v': v, 'kd': kd, 'sk': 'b', 'ad': 'n', 'aq': None, 'sc': 's', 'fr': 'a', 'ex': '-',
                              'cf': cf, 'cat': [CAT], 'r': 'because' if v != 'p' else ''})
            else:
                items.append({'i': n, 'dj': '小さな工房のサイト。', 'de': 'A small workshop site.', 'lj': None, 'le': None, 'ev': [],
                              'tg': ['工芸|craft'], 'cc': None, 'rc': None, 'q': None})
        return {'content': [{'type': 'text', 'text': json.dumps({'r': items})}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': self.tokens[0], 'output_tokens': self.tokens[1]}}


class JudgeStageTest(unittest.TestCase):
    def setUp(self):
        self.d1 = FakeD1()
        self.llm = FakeLLM()
        self.out = []

    def run_stage(self, **kw):
        kw.setdefault('limit', 100)
        kw.setdefault('apply', True)
        return js.run(self.d1, log=self.out.append, llm=self.llm, **kw)

    def test_transitions(self):
        for k in ('good.example', 'spam.example', 'thin.example', 'bad.example'):
            self.d1.add(k)
        self.d1.add('done.example', state='approved')
        self.d1.add('human.example', review_state='approved', description_ja='x')
        s = self.run_stage()
        self.assertEqual(self.d1.state('good.example'), 'approved')
        self.assertEqual(self.d1.cand('good.example')['review_state'], 'approved')
        self.assertEqual(self.d1.cand('good.example')['review_reason'], 'auto_approved')
        self.assertEqual(self.d1.cand('good.example')['description_en'], 'A small workshop site.')
        self.assertEqual(self.d1.state('spam.example'), 'rejected')
        self.assertTrue(self.d1.cand('spam.example')['review_reason'].startswith('spam:'))
        self.assertEqual(self.d1.state('thin.example'), 'candidate')          # low confidence -> a human looks
        self.assertEqual(self.d1.cand('thin.example')['review_state'], 'pending')
        self.assertEqual(self.d1.state('bad.example'), 'fetched')              # hard failure: retried later
        self.assertIn('judge_attempts=1', self.d1.cand('bad.example')['notes'])
        self.assertEqual(self.d1.state('human.example'), 'approved')           # moved without the LLM
        self.assertEqual(self.d1.state('done.example'), 'approved')
        self.assertEqual((s['approved'], s['rejected'], s['candidate'], s['retry_later']), (1, 1, 1, 1))
        self.assertEqual(s['moved_without_llm'], 1)

    def test_failure_cap_then_failed(self):
        self.d1.add('bad.example')
        for _ in range(js.MAX_JUDGE_ATTEMPTS):
            self.assertEqual(self.d1.state('bad.example'), 'fetched')
            self.run_stage()
        self.assertEqual(self.d1.state('bad.example'), 'failed')
        self.assertEqual(self.d1.cand('bad.example')['review_reason'], 'judge_failed')

    def test_refused_releases_reservation_and_stops_without_counting(self):
        for i in range(7):
            self.d1.add(f'refuse{i}.example')
        s = self.run_stage()
        rows = self.d1.query('SELECT kind, cost_usd FROM llm_ledger')
        self.assertTrue(rows and all(r['kind'] == 'release' and r['cost_usd'] == 0 for r in rows))
        self.assertEqual(len(self.llm.calls), 1)                              # the run stopped at the first API-wide refusal
        self.assertIn('refused', s['stopped'])
        for i in range(7):
            self.assertEqual(self.d1.state(f'refuse{i}.example'), 'fetched')
            self.assertFalse(self.d1.cand(f'refuse{i}.example')['notes'])     # no attempt counted

    def test_real_network_error_through_default_llm(self):
        from unittest import mock
        os.environ[KEY_ENV] = 'k'
        try:
            for k in ('a.example', 'b.example'):
                self.d1.add(k)
            with mock.patch.object(judge, 'http', side_effect=judge.AmbiguousRequest('network error on POST messages')):
                s = js.run(self.d1, limit=10, apply=True, log=self.out.append)      # must not raise
        finally:
            del os.environ[KEY_ENV]
        rows = self.d1.query('SELECT kind FROM llm_ledger')
        self.assertTrue(rows and all(r['kind'] == 'reserve' for r in rows))
        self.assertEqual(s['retry_later'], 2)
        self.assertIn('judge_attempts=1', self.d1.cand('a.example')['notes'])
        self.assertEqual(self.d1.state('a.example'), 'fetched')

    def test_api_error_through_default_llm_stops_run(self):
        from unittest import mock
        os.environ[KEY_ENV] = 'k'
        try:
            for i in range(15):
                self.d1.add(f'r{i}.example')
            with mock.patch.object(judge, 'http', side_effect=SystemExit('API error 401 on messages: invalid x-api-key')) as h:
                s = js.run(self.d1, limit=20, apply=True, log=self.out.append)
        finally:
            del os.environ[KEY_ENV]
        self.assertEqual(h.call_count, 1)
        self.assertTrue(s['stopped'])
        self.assertEqual(self.d1.query("SELECT COUNT(*) n FROM url_progress WHERE state='fetched'")[0]['n'], 15)
        self.assertEqual(self.d1.query("SELECT COUNT(*) n FROM url_candidates WHERE notes IS NOT NULL AND notes<>''")[0]['n'], 0)

    def test_ts_and_month_use_the_d1_clock(self):
        self.d1.add('good.example')
        self.run_stage()
        ins = [sql for sql in self.d1.log if sql.startswith('INSERT INTO llm_ledger')]
        self.assertTrue(ins)
        for sql in ins:
            self.assertIn('strftime(', sql)
            self.assertNotIn('substr(ts', sql)

    def test_claim_blocks_fresh_reviewing_and_reclaims_stale(self):
        self.d1.add('fresh.example', state='reviewing')
        self.d1.add('stale.example', state='reviewing')
        self.d1.query("UPDATE url_progress SET updated_at=datetime('now','-31 minutes') WHERE identity_key='stale.example'")
        self.d1.add('shown.example', state='reviewing', review_state='pending')
        self.d1.query("UPDATE url_progress SET updated_at=datetime('now','-90 minutes') WHERE identity_key='shown.example'")
        self.run_stage()
        self.assertEqual(self.d1.state('fresh.example'), 'reviewing')
        self.assertEqual(self.d1.state('stale.example'), 'approved')
        self.assertEqual(self.d1.state('shown.example'), 'reviewing')          # a human's queue is not ours

    def test_lost_claim_is_not_counted(self):
        self.d1.add('good.example')
        inner = self.llm

        def other_runner_wins(params):
            self.d1.query("UPDATE url_progress SET state='failed' WHERE identity_key='good.example'")
            return inner(params)
        s = js.run(self.d1, limit=10, apply=True, log=self.out.append, llm=other_runner_wins)
        self.assertEqual(self.d1.state('good.example'), 'failed')              # the other runner's state is kept
        self.assertEqual((s['judged'], s['approved'], s['lost_claim']), (0, 0, 1))

    def test_interrupted_move_of_reviewed_rows_is_picked_up_later(self):
        self.d1.add('p.example', review_state='pending', description_ja='a')
        self.d1.add('a.example', review_state='approved', description_ja='a')
        orig = self.d1.batch
        calls = []

        def dies(statements):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('process died')
            return orig(statements)
        self.d1.batch = dies
        self.run_stage()
        self.d1.batch = orig
        states = {k: self.d1.state(k) for k in ('p.example', 'a.example')}
        self.assertNotIn('reviewing', states.values())                          # never left claimed
        self.d1.query("UPDATE url_progress SET updated_at=datetime('now','-2 days')")
        self.run_stage()                                                        # two days later
        self.assertEqual(self.d1.state('a.example'), 'approved')
        self.assertEqual(self.d1.state('p.example'), 'candidate')
        self.assertEqual(self.llm.calls, [])

    def test_d1_error_around_reserve_gives_claim_back_and_reports(self):
        self.d1.add('good.example')
        orig = self.d1.query

        def flaky(sql, params=()):
            if sql.startswith('INSERT INTO llm_ledger'):
                raise RuntimeError('d1 down')
            return orig(sql, params)
        self.d1.query = flaky
        s = self.run_stage()
        self.d1.query = orig
        self.assertIn('D1 error', s['stopped'])
        self.assertFalse(s['budget_stopped'])
        self.assertEqual(self.d1.state('good.example'), 'fetched')
        self.assertFalse(self.d1.cand('good.example')['notes'])

    def test_second_runner_gets_nothing(self):
        self.d1.add('good.example', state='reviewing')                         # runner A holds it
        s = self.run_stage()
        self.assertEqual((s['selected'], len(self.llm.calls)), (0, 0))

    def test_settle_error_stops_other_workers(self):
        for i in range(20):
            self.d1.add(f'site{i:02d}.example')
        self.d1.fail_settle = True
        with self.assertRaises(js.LedgerSettleError):
            self.run_stage(concurrency=2)
        self.assertLessEqual(len(self.llm.calls), 3)                           # 8 requests without the stop

    def test_budget_stop_mid_chunk_keeps_paid_triage_rejects(self):
        self.d1.add('spam.example')
        self.d1.add('good.example')
        inner = self.llm

        def spend_after_triage(params):
            r = inner(params)
            if 'STAGE 1' in params['system'][0]['text']:
                self.d1.query("INSERT INTO llm_ledger (id,ts,kind,cost_usd) VALUES ('big', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'call', 99.99)")
            return r
        s = js.run(self.d1, limit=10, apply=True, log=self.out.append, llm=spend_after_triage)
        self.assertEqual(self.d1.state('spam.example'), 'rejected')            # already paid for
        self.assertEqual(self.d1.state('good.example'), 'fetched')
        self.assertFalse(self.d1.cand('good.example')['notes'])
        self.assertTrue(s['budget_stopped'])

    def test_refusal_three_times_goes_to_a_human(self):
        self.d1.add('r.example')
        llm = lambda params: {'content': [], 'stop_reason': 'refusal', 'usage': {'input_tokens': 5, 'output_tokens': 1}}  # noqa: E731
        for _ in range(js.MAX_JUDGE_ATTEMPTS):
            js.run(self.d1, limit=5, apply=True, log=self.out.append, llm=llm)
        self.assertEqual(self.d1.state('r.example'), 'candidate')
        self.assertEqual(self.d1.cand('r.example')['review_reason'], 'judge_refused')

    def test_write_failure_counts_an_attempt(self):
        self.d1.add('good.example')
        orig = self.d1.batch

        def flaky(statements):
            if any(sql.startswith('UPDATE url_candidates SET input_hash') for sql, _ in statements):
                raise RuntimeError('d1 write down')
            return orig(statements)
        self.d1.batch = flaky
        s = self.run_stage()
        self.assertEqual(s['write_failed'], 1)
        self.assertIn('judge_attempts=1', self.d1.cand('good.example')['notes'])
        self.assertEqual(self.d1.state('good.example'), 'fetched')

    def test_no_answer_keeps_reservation(self):
        self.d1.add('good.example')

        def boom(params):
            raise OSError('network down')
        js.run(self.d1, limit=10, apply=True, log=self.out.append, llm=boom)
        rows = self.d1.query('SELECT kind, cost_usd FROM llm_ledger')
        self.assertTrue(rows and all(r['kind'] == 'reserve' and r['cost_usd'] > 0 for r in rows))

    def test_ledger_reserve_then_settle_consistency(self):
        self.d1.add('good.example')
        s = self.run_stage()
        rows = self.d1.query('SELECT * FROM llm_ledger')
        self.assertEqual(len(rows), 2)                                         # triage + describe, one row each
        self.assertTrue(all(r['kind'] == 'call' for r in rows))
        self.assertEqual({r['model'] for r in rows}, {js.TRIAGE_MODEL, js.DESCRIBE_MODEL})
        expect = sum(judge.usage_cost(r['model'], {'input_tokens': 1000, 'output_tokens': 200}) for r in rows)
        self.assertAlmostEqual(sum(r['cost_usd'] for r in rows), expect, places=5)
        self.assertAlmostEqual(s['cost_usd'], expect, places=5)
        self.assertTrue(all(r['input_tokens'] == 1000 and r['output_tokens'] == 200 for r in rows))

    def test_budget_refusal_at_cap(self):
        self.d1.add('good.example')
        self.d1.query("INSERT INTO llm_ledger (id,ts,kind,cost_usd) VALUES ('old', ?, 'call', 99.999999)", [js.now_iso()])
        s = self.run_stage()
        self.assertTrue(s['budget_stopped'])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.d1.state('good.example'), 'fetched')
        self.assertFalse(self.d1.cand('good.example')['notes'])                # not counted as a failed attempt
        self.assertEqual(len(self.d1.query('SELECT id FROM llm_ledger')), 1)   # nothing reserved

    def test_other_month_does_not_count(self):
        self.d1.add('good.example')
        self.d1.query("INSERT INTO llm_ledger (id,ts,kind,cost_usd) VALUES ('old', '2020-01-01T00:00:00Z', 'call', 500)")
        self.assertFalse(self.run_stage()['budget_stopped'])

    def test_failed_settle_keeps_reservation(self):
        self.d1.add('good.example')
        self.d1.fail_settle = True
        with self.assertRaises(js.LedgerSettleError):
            self.run_stage()
        rows = self.d1.query('SELECT kind, cost_usd FROM llm_ledger')
        self.assertEqual([r['kind'] for r in rows], ['reserve'])
        self.assertGreater(rows[0]['cost_usd'], 0)
        self.assertEqual(self.d1.state('good.example'), 'fetched')

    def test_rerun_is_idempotent_no_double_charge(self):
        for k in ('good.example', 'spam.example'):
            self.d1.add(k)
        self.run_stage()
        n_calls, n_rows = len(self.llm.calls), len(self.d1.query('SELECT id FROM llm_ledger'))
        s = self.run_stage()
        self.assertEqual(len(self.llm.calls), n_calls)
        self.assertEqual(len(self.d1.query('SELECT id FROM llm_ledger')), n_rows)
        self.assertEqual(s['selected'], 0)

    def test_already_reviewed_not_rejudged(self):
        self.d1.add('x.example', review_state='pending', description_ja='a', input_hash='h')
        self.run_stage()
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.d1.cand('x.example')['input_hash'], 'h')
        self.assertEqual(self.d1.state('x.example'), 'candidate')

    def test_dry_run_writes_nothing_calls_nothing(self):
        self.d1.add('good.example')
        s = self.run_stage(apply=False)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.d1.writes(), [])
        self.assertTrue(s['dry_run'])
        self.assertEqual(s['would_call_llm_for'], 1)
        self.assertEqual(self.d1.state('good.example'), 'fetched')

    def test_param_limit_and_concurrency(self):
        for i in range(12):
            self.d1.add(f'site{i}.example')
        s = self.run_stage(concurrency=3)          # FakeD1 asserts <= 100 params on every statement
        self.assertEqual(s['approved'], 12)
        self.assertEqual(self.d1.query("SELECT COUNT(*) AS n FROM llm_ledger WHERE kind!='call'")[0]['n'], 0)

    def test_no_files_and_key_never_logged(self):
        os.environ[KEY_ENV] = 'sk-ant-SECRET-VALUE'
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cwd = os.getcwd()
                os.chdir(tmp)
                try:
                    self.d1.add('good.example')
                    self.d1.add('bad.example')
                    self.run_stage()
                    self.assertEqual(os.listdir(tmp), [])
                finally:
                    os.chdir(cwd)
            blob = '\n'.join(map(str, self.out)) + json.dumps(self.llm.calls, ensure_ascii=False)
            self.assertNotIn('SECRET-VALUE', blob)
            self.assertNotIn('SECRET-VALUE', json.dumps(self.d1.query('SELECT * FROM llm_ledger')))
        finally:
            del os.environ[KEY_ENV]

    def test_missing_key_stops_before_any_write(self):
        self.d1.add('good.example')
        os.environ.pop(KEY_ENV, None)
        with self.assertRaises(RuntimeError):
            js.run(self.d1, limit=5, apply=True, log=self.out.append)
        self.assertEqual(self.d1.writes(), [])


if __name__ == '__main__':
    unittest.main()
