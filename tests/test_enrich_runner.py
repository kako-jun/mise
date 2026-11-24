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
        # the judge's own stale claim (candidate row fetched by the fetch stage) is tolerated
        self.d1.db.execute("UPDATE url_candidates SET state='fetched'")
        self.assertEqual(eo.stale_reviewing(self.d1), [])

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
