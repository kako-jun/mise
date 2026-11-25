"""Enrich runner (scripts/enrich_online.py). Fake D1 = in-memory SQLite built from the repo's real migrations; fake fetch fn and fake LLM are injected.
No network, no production D1, no local files."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import enrich_online as eo
from enrich import judge_stage as js
from enrich import publish_stage as ps
import discover_online as do
from progress_identity import progress_identity_key
from test_enrich_publish_stage import FakeD1, add_candidate
from test_enrich_judge_stage import FakeLLM
from test_enrich_fetch_stage import ok

URLS = {'https://taro-home.jp/': {}, 'https://yamada-taro.jp/': {}, 'https://adult-corner.jp/': {}}


class AdultLLM(FakeLLM):
    """FakeLLM, but 'adult' hosts are flagged adult=yes by the triage."""
    def __call__(self, params):
        out = super().__call__(params)
        text = params['messages'][0]['content']
        if 'STAGE 1' in params['system'][0]['text']:
            import re
            urls = re.findall(r'url: (\S+)', text)
            r = json.loads(out['content'][0]['text'])
            for item, u in zip(r['r'], urls):
                if 'adult' in u:
                    item['ad'], item['aq'] = 'y', 'あああああ'
            out['content'][0]['text'] = json.dumps(r)
        return out


def fake_fetch(url):
    return {**ok(), 'final_url': url}


def snapshot(d1):
    return {t: d1.db.execute(f'SELECT * FROM {t} ORDER BY 1,2').fetchall() for t in ('url_progress', 'url_candidates', 'sites', 'llm_ledger', 'discovery_cursors')}


class Runner(unittest.TestCase):
    def setUp(self):
        self.d1 = FakeD1()
        self.out = []
        self.base_sites = self.d1.one('SELECT COUNT(*) FROM sites')[0]

    def seed(self, urls=URLS):
        for u in urls:
            self.d1.db.execute("INSERT INTO url_progress(identity_key,url,state) VALUES(?,?,'discovered')",
                               (progress_identity_key(u), u))

    def go(self, **kw):
        kw.setdefault('apply', True)
        kw.setdefault('fetch', fake_fetch)
        kw.setdefault('llm', AdultLLM())
        kw.setdefault('bookmark_fn', lambda c: 'bm-123')
        code, start, end = eo.run_pipeline(self.d1, out=self.out.append, **kw)
        return code, start, end

    def text(self):
        return '\n'.join(map(str, self.out))

    def lease_rows(self):
        return self.d1.db.execute("SELECT * FROM discovery_cursors WHERE source='lease'").fetchall()

    def test_full_path_three_sites(self):
        self.seed()
        code, start, end = self.go()
        self.assertEqual(code, 0, self.text())
        self.assertEqual(end['published'] - start['published'], 3)
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites + 3)
        sid = ps.site_id('adult-corner.jp')
        self.assertEqual(self.d1.one('SELECT adult_content FROM sites WHERE id=?', (sid,))[0], 1)
        self.assertEqual(tuple(self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id=? AND kind='thumbnail'", (sid,))), ('excluded', 0))
        self.assertIsNotNone(self.d1.one('SELECT 1 FROM sites WHERE id=?', (ps.site_id('yamada-taro.jp'),)))
        self.assertEqual(self.lease_rows(), [])
        self.assertIn('Time Travel bookmark = bm-123', self.text())
        self.assertIn('残りなし', self.text())
        self.assertIn('url_progress', self.text())

    def test_dry_run_writes_nothing(self):
        self.seed()
        before = snapshot(self.d1)
        w = self.d1.writes
        code, _, _ = self.go(apply=False, llm=None, fetch=lambda u: self.fail('network'))
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.d1), before)
        self.assertEqual(self.d1.writes, w)

    def test_lease_exclusive_and_released(self):
        self.seed()
        other = do.Lease(self.d1, owner='other', key=eo.LEASE_KEY)
        self.assertTrue(other.acquire())
        code, _, _ = self.go()
        self.assertEqual(code, 2)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='discovered'")[0], 3)
        other.release()
        self.assertEqual(self.go()[0], 0)
        self.assertEqual(self.lease_rows(), [])
        # does not collide with the discovery lease
        d = do.Lease(self.d1, owner='d')
        self.assertTrue(d.acquire())
        self.seed({'https://zeta-site.jp/': {}})
        self.assertEqual(self.go()[0], 0)
        d.release()

    def test_lease_lost_exit_4(self):
        self.seed()
        d1 = self.d1

        inner = AdultLLM()

        def steal(params):   # the judge runs in the main thread; the lease is taken over mid-run
            d1.db.execute("UPDATE discovery_cursors SET cursor_value='thief|9999999999' WHERE source='lease' AND cursor_key='enrich'")
            return inner(params)
        code, _, _ = self.go(llm=steal)
        self.assertEqual(code, 4)
        self.assertIn('lease lost', self.text())

    def test_max_seconds(self):
        self.seed()
        t = [0.0]

        def clock():
            t[0] += 100
            return t[0]
        code, start, end = self.go(max_seconds=1, clock=clock, limits={'fetch': 1, 'judge': 1, 'publish': 1})
        self.assertEqual(code, 0)
        self.assertIn('--max-seconds', self.text())
        self.assertLess(end['published'] - start['published'], 3)

    def test_rounds_limit(self):
        self.seed()
        code, start, end = self.go(rounds=1, limits={'fetch': 1, 'judge': 1, 'publish': 1})
        self.assertEqual(code, 0)
        self.assertIn('--rounds 1', self.text())
        self.assertEqual(end['published'] - start['published'], 1)

    def test_budget_exhausted_stops_cleanly(self):
        self.seed()
        self.d1.db.execute("INSERT INTO llm_ledger (id,ts,kind,cost_usd) VALUES ('big', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'call', 99.9999)")
        llm = AdultLLM()
        code, start, end = self.go(llm=llm)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(llm.calls, [])
        self.assertEqual(end['fetched'], 3)
        self.assertEqual(end['published'] - start['published'], 0)
        self.assertEqual(self.lease_rows(), [])

    def test_stage_exception_releases_lease(self):
        self.seed()

        def boom(url):
            raise ValueError('kaboom')
        # fetch stage turns a fetch exception into a retry; a crash in judge propagates
        def bad_llm(params):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.go(llm=bad_llm)
        self.assertEqual(self.lease_rows(), [])
        self.d1.db.execute("INSERT INTO url_progress(identity_key,url,state) VALUES('x.jp/','https://x.jp/','discovered')")
        orig = eo.fetch_stage.run
        eo.fetch_stage.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('d1 exploded'))
        try:
            with self.assertRaises(RuntimeError):
                self.go()
        finally:
            eo.fetch_stage.run = orig
        self.assertEqual(self.lease_rows(), [])

    def test_rate_limit_exit_3(self):
        self.seed()
        orig = eo.fetch_stage.run
        eo.fetch_stage.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Cloudflare API 429: slow down'))
        try:
            code, _, _ = self.go()
        finally:
            eo.fetch_stage.run = orig
        self.assertEqual(code, 3)
        self.assertEqual(self.lease_rows(), [])

    def test_preflight_refuses_stale_reviewing(self):
        add_candidate(self.d1, 'old.example.jp', state='reviewing', review_state='')
        self.d1.db.execute("UPDATE url_progress SET updated_at='2000-01-01 00:00:00'")
        self.d1.db.execute("UPDATE url_candidates SET state='deferred'")
        code, _, _ = self.go()
        self.assertEqual(code, 1)
        self.assertIn('reviewing', self.text())
        self.assertEqual(self.lease_rows(), [])
        # state of the candidate row does not matter; a claim updated within 30 minutes (the judge's own) is ignored
        self.d1.db.execute("UPDATE url_candidates SET state='fetched'")
        self.assertEqual(eo.stale_reviewing(self.d1), ['old.example.jp/'])
        self.d1.db.execute("UPDATE url_progress SET updated_at=datetime('now')")
        self.assertEqual(eo.stale_reviewing(self.d1), [])

    def test_preflight_check_only_with_judge_stage(self):
        add_candidate(self.d1, 'old.example.jp', state='reviewing', review_state='')
        self.d1.db.execute("UPDATE url_progress SET updated_at='2000-01-01 00:00:00'")
        self.assertEqual(self.go(stages=('fetch', 'publish'))[0], 0)

    def test_preflight_reports_approved_without_review_state(self):
        add_candidate(self.d1, 'odd.example.jp', review_state='pending')
        self.go(apply=False, llm=None)
        self.assertIn('review_state が approved でない行 = 1', self.text())

    def test_bookmark_failure_warns_and_continues(self):
        def bad(c):
            raise RuntimeError('nope')
        code, _, _ = self.go(bookmark_fn=bad)
        self.assertEqual(code, 0)
        self.assertIn('WARNING', self.text())

    def test_publish_anomaly_guard_held_ratio(self):
        for i in range(10):
            add_candidate(self.d1, f's{i}.example.jp', description_en='')   # every row fails a gate -> held
        code, _, _ = self.go(stages=('publish',))
        self.assertEqual(code, 1)
        self.assertIn('ゲート保留が異常', self.text())

    def test_integrity_violation_exit_1_with_ids(self):
        add_candidate(self.d1)
        orig = ps.post_check
        ps.post_check = lambda c, ids: {'uncategorized': [(ids[0],)]}
        try:
            code, _, _ = self.go(stages=('publish',))
        finally:
            ps.post_check = orig
        self.assertEqual(code, 1)
        self.assertIn(ps.site_id('alpha.example.jp'), self.text())

    def test_no_local_files_created(self):
        self.seed()
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            tmp = tempfile.gettempdir()
            before_tmp = set(os.listdir(tmp))
            try:
                self.go()
                self.assertEqual(os.listdir(d), [])
            finally:
                os.chdir(old)
            self.assertEqual(set(os.listdir(tmp)) - before_tmp - {Path(d).name}, set())

    def test_main_args_and_injection(self):
        self.seed()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = eo.main(['--apply', '--stages', 'fetch', '--fetch-limit', '2'], client=self.d1, fetch=fake_fetch, bookmark_fn=lambda c: 'b')
        self.assertEqual(code, 0)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='fetched'")[0], 3)
        with self.assertRaises(SystemExit):
            eo.main(['--stages', 'bogus'], client=self.d1)


NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')"


class Guards(Runner):
    def test_limit_cap(self):
        with self.assertRaises(ValueError):
            self.go(limits={'fetch': eo.MAX_LIMIT + 1})
        with self.assertRaises(SystemExit):
            eo.main(['--limit', str(eo.MAX_LIMIT + 1)], client=self.d1)

    def test_heartbeat_judge_lease_lost_exit_4(self):
        for i in range(12):
            self.seed({f'https://site{i}-home.jp/': {}})
        d1, inner, t = self.d1, AdultLLM(), [0.0]

        def clock():
            t[0] += 200   # always past the heartbeat interval
            return t[0]

        def steal(params):
            d1.db.execute("UPDATE discovery_cursors SET cursor_value='thief|9999999999' WHERE source='lease' AND cursor_key='enrich'")
            return inner(params)
        code, _, _ = self.go(llm=steal, clock=clock, stages=('fetch', 'judge'))
        self.assertEqual(code, 4)
        self.assertLessEqual(len(inner.calls), 2)   # the first chunk only (triage + describe); no further chunk after the loss
        self.assertEqual(d1.one("SELECT COUNT(*) FROM url_progress WHERE state='reviewing'")[0], 0)   # the claim was given back
        done = d1.db.execute("SELECT COUNT(*) FROM url_progress p JOIN url_candidates c USING(identity_key) WHERE p.state<>'fetched' AND p.state<>'discovered' "
                             "AND c.review_state IN ('approved','pending','rejected') AND c.identity_key LIKE 'site%-home.jp/'").fetchone()[0]
        self.assertGreaterEqual(done, 1)   # the first chunk's paid result was written before the claim was given back
        # restart 31 minutes later (the thief is gone): not blocked by the pre-flight, finishes
        d1.db.execute("DELETE FROM discovery_cursors")
        d1.db.execute("UPDATE url_progress SET updated_at=datetime('now','-31 minutes') WHERE state IN ('fetched','reviewing','approved','candidate')")
        self.out.clear()
        self.assertEqual(self.go()[0], 0, self.text())
        self.assertEqual(d1.one("SELECT COUNT(*) FROM url_progress WHERE state='reviewing'")[0], 0)

    def test_heartbeat_fetch_and_publish_stop(self):
        for i in range(7):
            self.seed({f'https://f{i}-home.jp/': {}})

        class Lost(Exception):
            pass
        calls = []

        def hb():
            calls.append(1)
            if len(calls) == 2:
                raise Lost()
        with self.assertRaises(Lost):
            eo.fetch_stage.run(self.d1, limit=10, apply=True, log=lambda *_: None, fetch=fake_fetch, heartbeat=hb)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='fetched'")[0], 5)   # only the first claim group
        d1 = FakeD1()
        add_candidate(d1, 'a.example.jp'); add_candidate(d1, 'b.example.jp')
        n = []

        def hb2():
            n.append(1)
            if len(n) == 2:
                raise Lost()
        with self.assertRaises(Lost) as cm:
            ps.run(d1, limit=10, apply=True, log=lambda *_: None, heartbeat=hb2)
        self.assertEqual(len(cm.exception.result['ids']), 1)

    def test_judge_cost_guard_skips_judge_keeps_publish_exit_1(self):
        for i in range(eo.JUDGE_GUARD_WINDOW):
            self.d1.db.execute("INSERT INTO url_candidates(identity_key,review_state,judged_at) VALUES(?, 'rejected', strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour'))", (f'old{i}.jp/',))
        self.seed()
        add_candidate(self.d1, 'ready.example.jp')
        llm = AdultLLM()
        code, start, end = self.go(llm=llm)
        self.assertEqual(code, 1, self.text())
        self.assertEqual(llm.calls, [])
        self.assertIsNotNone(self.d1.one('SELECT 1 FROM sites WHERE id=?', (ps.site_id('ready.example.jp'),)))   # publish was not stopped
        self.assertEqual(end['fetched'] - start['fetched'], 3)
        self.assertEqual(self.lease_rows(), [])

    def test_judge_guard_needs_full_window_and_one_approval(self):
        for i in range(eo.JUDGE_GUARD_WINDOW):
            self.d1.db.execute("INSERT INTO url_candidates(identity_key,review_state,judged_at) VALUES(?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour'))",
                               (f'old{i}.jp/', 'approved' if i == 0 else 'rejected'))
        self.assertFalse(eo.judge_unapproved_window(self.d1))
        self.d1.db.execute("DELETE FROM url_candidates WHERE identity_key='old0.jp/'")
        self.assertFalse(eo.judge_unapproved_window(self.d1))   # window not full
        self.d1.db.execute("INSERT INTO url_candidates(identity_key,review_state,judged_at) VALUES('x/', 'rejected', strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
        self.assertTrue(eo.judge_unapproved_window(self.d1))
        self.d1.db.execute("UPDATE url_candidates SET judged_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 hours') WHERE judged_at IS NOT NULL")
        self.assertFalse(eo.judge_unapproved_window(self.d1))   # recovers by itself once the window is empty

    def test_guard_still_moves_rows_without_llm(self):
        for i in range(eo.JUDGE_GUARD_WINDOW):
            self.d1.db.execute(f"INSERT INTO url_candidates(identity_key,review_state,judged_at) VALUES(?, 'rejected', {NOW_SQL})", (f'old{i}.jp/',))
        add_candidate(self.d1, 'human.example.jp', state='fetched', review_state='approved')
        self.seed({'https://fresh-home.jp/': {}})
        llm = AdultLLM()
        code, _, _ = self.go(llm=llm)
        self.assertEqual(code, 1)
        self.assertEqual(llm.calls, [])
        self.assertIsNotNone(self.d1.one('SELECT 1 FROM sites WHERE id=?', (ps.site_id('human.example.jp'),)))   # moved without LLM, then published
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='fresh-home.jp/'")[0], 'fetched')

    def test_judge_llm_disabled_needs_no_key(self):
        d1 = FakeD1()
        add_candidate(d1, 'm.example.jp', state='fetched', review_state='rejected')
        add_candidate(d1, 'n.example.jp', state='fetched', review_state='')
        os.environ.pop('ANTHROPIC_API_KEY', None)
        s = js.run(d1, limit=10, apply=True, log=lambda *_: None, llm_enabled=False)
        self.assertEqual((s['moved_without_llm'], s['judged']), (1, 0))
        self.assertEqual(d1.one("SELECT state FROM url_progress WHERE identity_key='n.example.jp/'")[0], 'fetched')

    def test_judge_stop_rate_limit_exit_3(self):
        self.seed()
        orig = eo.judge_stage.run
        eo.judge_stage.run = lambda *a, **k: {'selected': 1, 'stopped': 'API refused the request: HTTP 429 rate limit', 'budget_stopped': False}
        try:
            self.assertEqual(self.go()[0], 3)
        finally:
            eo.judge_stage.run = orig

    def test_limit_zero_is_an_error(self):
        for a in (['--limit', '0'], ['--judge-limit', '0']):
            with self.assertRaises(SystemExit):
                eo.main(a, client=self.d1)

    def test_heartbeat_tolerates_transient_errors(self):
        class L:
            n = 0

            def renew(self):
                self.n += 1
                if self.n <= 2:
                    raise RuntimeError('d1 blip')
        hb = eo.Heartbeat(L(), lambda: 0.0)
        hb.now(); hb.now()          # two blips: tolerated
        hb.now()                    # success resets
        l2 = L(); l2.n = -10
        hb2 = eo.Heartbeat(l2, lambda: 0.0)
        hb2.now(); hb2.now()
        with self.assertRaises(do.LeaseLost):
            hb2.now()               # third in a row

    def test_preflight_refuses_old_reserve_exit_5(self):
        self.d1.db.execute("INSERT INTO llm_ledger(id,ts,kind,cost_usd) VALUES('r', strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 hours'), 'reserve', 0.5)")
        code, _, _ = self.go()
        self.assertEqual(code, 5)
        self.assertEqual(self.lease_rows(), [])
        self.d1.db.execute("UPDATE llm_ledger SET ts=strftime('%Y-%m-%dT%H:%M:%SZ','now')")   # a fresh (in-flight) reserve is fine
        self.assertEqual(self.go(stages=('publish',))[0], 0)

    def test_publish_batch_failure_guard(self):
        for i in range(eo.PUBLISH_MAX_ERRORS):
            add_candidate(self.d1, f'e{i}.example.jp', tags=json.dumps(['爆|boom']))
        self.d1.db.execute("CREATE TRIGGER poison BEFORE INSERT ON site_tags WHEN NEW.label_ja='爆' BEGIN SELECT RAISE(ABORT,'poison'); END")
        code, _, _ = self.go(stages=('publish',))
        self.assertEqual(code, 1)
        self.assertIn('バッチ失敗が異常', self.text())

    def test_daily_publish_cap_from_d1(self):
        for i in range(3):
            add_candidate(self.d1, f'c{i}.example.jp')
        recent = eo.published_last_day(self.d1)
        code, start, end = self.go(stages=('publish',), max_publish_per_day=recent + 2)
        self.assertEqual(code, 0)
        self.assertEqual(end['published'] - start['published'], 2)
        self.assertIn('日次', self.text())
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='approved'")[0], 1)

    def test_max_publish_per_day_default_and_flag(self):
        self.assertEqual(eo.MAX_PUBLISH_PER_DAY, 5000)
        seen = {}
        orig = eo.run_pipeline
        eo.run_pipeline = lambda client, **k: (seen.update(k), (0, None, None))[1]
        try:
            self.assertEqual(eo.main([], client=self.d1), 0)
            self.assertEqual(seen['max_publish_per_day'], 5000)
            self.assertEqual(eo.main(['--max-publish-per-day', '123'], client=self.d1), 0)
            self.assertEqual(seen['max_publish_per_day'], 123)
            for bad in ('0', '-5'):
                with self.assertRaises(SystemExit) as cm:
                    eo.main(['--max-publish-per-day', bad], client=self.d1)
                self.assertEqual(cm.exception.code, 2)
        finally:
            eo.run_pipeline = orig

    def test_ledger_settle_error_exit_5_releases_lease(self):
        self.seed()
        orig = eo.judge_stage.run
        eo.judge_stage.run = lambda *a, **k: (_ for _ in ()).throw(js.LedgerSettleError('settle failed'))
        try:
            code, _, _ = self.go()
        finally:
            eo.judge_stage.run = orig
        self.assertEqual(code, 5)
        self.assertEqual(self.lease_rows(), [])
        self.assertIn('再試行しない', self.text())

    def test_no_progress_stops_with_warning_exit_0(self):
        self.seed()
        orig = eo.fetch_stage.run
        eo.fetch_stage.run = lambda *a, **k: {'selected': 3}
        try:
            code, _, _ = self.go(stages=('fetch',))
        finally:
            eo.fetch_stage.run = orig
        self.assertEqual(code, 0)
        self.assertIn('WARNING 停止', self.text())

    def test_enrich_and_discover_leases_coexist(self):
        a, b = do.Lease(self.d1, owner='A'), do.Lease(self.d1, owner='B', key=eo.LEASE_KEY)
        self.assertTrue(a.acquire() and b.acquire())
        self.assertEqual(sorted(r['cursor_key'] for r in self.lease_rows()), ['discover', 'enrich'])
        b.release()
        self.assertEqual([r['cursor_key'] for r in self.lease_rows()], ['discover'])

    def test_rate_limit_detection(self):
        class H(Exception):
            code = 429
        self.assertTrue(eo.is_rate_limit(H()))
        e = RuntimeError('wrapped'); e.__cause__ = H()
        self.assertTrue(eo.is_rate_limit(e))
        self.assertTrue(eo.is_rate_limit(RuntimeError('Cloudflare API 429: x')))
        self.assertTrue(eo.is_rate_limit(RuntimeError('HTTP 429')))
        self.assertFalse(eo.is_rate_limit(RuntimeError('selected 14290 rows')))

    def test_integrity_violation_visible_when_publish_raises(self):
        add_candidate(self.d1, 'ok.example.jp'); add_candidate(self.d1, 'bad.example.jp', description_en='')
        self.d1.db.execute("UPDATE url_progress SET updated_at='2000-01-01' WHERE identity_key='ok.example.jp/'")
        orig_batch = self.d1.batch
        self.d1.batch = lambda st: (_ for _ in ()).throw(RuntimeError('hold down')) if any('review_reason' in x for x, _ in st) else orig_batch(st)
        orig = ps.post_check
        ps.post_check = lambda c, ids: {'uncategorized': [(ids[0],)]}
        try:
            code, _, _ = self.go(stages=('publish',))
        finally:
            ps.post_check = orig
        self.assertEqual(code, 1)
        self.assertIn(ps.site_id('ok.example.jp'), self.text())


class PublishNits(unittest.TestCase):
    def test_only_review_state_approved_is_selected(self):
        d1 = FakeD1()
        add_candidate(d1, review_state='pending')
        res = ps.run(d1, limit=10, apply=True, log=lambda *_: None)
        self.assertEqual((res['selected'], res['published']), (0, 0))
        self.assertEqual(d1.one("SELECT state FROM url_progress WHERE identity_key='alpha.example.jp/'")[0], 'approved')

    def test_post_check_runs_when_loop_raises(self):
        d1 = FakeD1()
        add_candidate(d1)
        add_candidate(d1, 'beta.example.jp', description_en='')   # gate -> hold batch
        d1.db.execute("UPDATE url_progress SET updated_at='2000-01-01' WHERE identity_key='alpha.example.jp/'")
        orig_batch, calls, logs = d1.batch, [], []

        def batch(stmts):
            if any('review_reason' in s for s, _ in stmts):
                raise RuntimeError('hold batch down')
            return orig_batch(stmts)
        d1.batch = batch
        orig = ps.post_check
        ps.post_check = lambda c, ids: calls.append(list(ids)) or {}
        try:
            with self.assertRaises(RuntimeError):
                ps.run(d1, limit=10, apply=True, log=logs.append)
        finally:
            ps.post_check = orig
        self.assertEqual(calls, [[ps.site_id('alpha.example.jp')]])
        self.assertTrue(any(l.startswith('publish_stage:') for l in logs))


if __name__ == '__main__':
    unittest.main()
