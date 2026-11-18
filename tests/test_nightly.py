"""夜間ジョブ（#124）。外部コマンド（subprocess）・API・ネットワークは全てモック。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import frontier
import judge
import nightly
import integrity

JCOLS = ("key, input_hash, origin, model, judged_at, imported_at, spam_verdict, spam_kind, spam_reason, categories, tags, adult, "
         "site_kind, site_kind_reason, site_scope, confidence, review_state")


def add_site(db, key, state='candidate', judged=None):
    db.execute("INSERT INTO frontier (key, host, url, source, discovered_at, state, title, text_head) VALUES (?,?,?,?,?,?,?,?)",
               (key, key, f'https://{key}/', 'links', '2026-11-01T00:00:00+00:00', state, 't', 'body'))
    if judged:
        db.execute(f"INSERT INTO llm_judgement ({JCOLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, 'h', 'pipeline', 'm', 'x', 'x', 'pass', 'none', '', '["astronomy"]', '[]', 'no', 'individual', '', 'standalone', 'high', judged))
    db.commit()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.var = t / 'var'
        self.db_path = t / 'f.sqlite'
        self.ledger = t / 'ledger.jsonl'
        self.db = frontier.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.calls = []
        self.worst = 1.0
        self.integrity_rc = 0          # 投入後の本番整合チェック
        self.integrity_before_rc = 0   # 投入前の本番整合チェック
        self.llm_rc = 0
        self.exported = 2
        self.p_run = mock.patch.object(nightly, 'run_cmd', side_effect=self.fake)
        self.p_run.start()
        self.addCleanup(self.p_run.stop)
        self.violations = {}
        self.p_gate = mock.patch.object(integrity, 'gate', side_effect=lambda sql, current=None: self.violations)
        self.p_gate.start()
        self.addCleanup(self.p_gate.stop)
        self.p_env = mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'dummy'})
        self.p_env.start()
        self.addCleanup(self.p_env.stop)

    def args(self, *extra, date='2026-11-18'):
        return nightly.parse_args(['--date', date, '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger), *extra])

    def fake(self, cmd, env=None, timeout=None):
        self.calls.append(cmd)
        s = ' '.join(cmd)
        if 'wrangler' in s and 'time-travel' in s:
            return 0, '{"bookmark": "00000085-0000024c-00004c6d-aaaabbbbccccdddd"}'
        if 'wrangler' in s:
            return 0, 'ok'
        if 'check-data-integrity' in s:
            rc = self.integrity_rc if any('execute' in c for c in self.calls) else self.integrity_before_rc
            return rc, 'all error-level checks passed' if not rc else '[FAIL] x'
        if 'pipeline.py' in s and '--dry-run' in cmd:
            return 0, f'TOTAL expected ${self.worst / 2:.4f}, worst case ${self.worst:.4f}'
        if 'llm/pipeline.py' in s:
            Path(cmd[cmd.index('--out') + 1]).write_text('{"results": []}')
            return self.llm_rc, ''
        if 'import_results' in s:
            return 0, 'imported=1'
        if 'approve_policy' in s:
            return 0, 'applied: approve=2, hold=1, reject_stale=0'
        if 'export_approved_sql' in s:
            out = Path(cmd[cmd.index('--out') + 1])
            keys = (out.parent / 'keys.txt').read_text().split()[:self.exported]
            out.write_text('-- Approved sites\n' + ''.join(f'-- {k}\nINSERT 1;\n' for k in keys))
            return 0, f'approved=2, exported={self.exported} -> out'
        return 0, '{}'

    def wrangler_calls(self):
        return [c for c in self.calls if 'wrangler' in ' '.join(c) or '--remote' in c]

    def status(self, date='2026-11-18'):
        return json.loads((self.var / 'nightly' / date / 'status.json').read_text())

    def seed(self):
        add_site(self.db, 'a.jp')
        add_site(self.db, 'b.jp')
        add_site(self.db, 'c.jp', judged='approved')
        add_site(self.db, 'd.jp', judged='approved')
        add_site(self.db, 'e.jp', judged='approved')


class Plan(Env):
    def test_dry_run_touches_nothing(self):
        self.seed()
        out_before = set(Path(self.tmp.name).rglob('*'))
        self.assertEqual(nightly.main(['--dry-run', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)]), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.var.exists())
        self.assertEqual(out_before, set(Path(self.tmp.name).rglob('*')))

    def test_dry_run_prints_plan(self):
        self.seed()
        with mock.patch('builtins.print') as pr:
            nightly.main(['--dry-run', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)])
        text = pr.call_args[0][0]
        self.assertIn('今夜の判定対象（未判定の candidate）: 2 件', text)
        self.assertIn('予算ガード: 発動しない見込み', text)
        self.assertIn('実行しない(--apply-prod なし)', text)

    def test_dry_run_and_apply_prod_conflict(self):
        with self.assertRaises(SystemExit):
            self.args('--dry-run', '--apply-prod')

    def test_only_unjudged_candidates(self):
        self.seed()
        self.assertEqual(sorted(c['id'] for c in nightly.unjudged_candidates(str(self.db_path))), ['a_jp', 'b_jp'])


class Budget(Env):
    def write_ledger(self, rows):
        self.ledger.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_month_total_is_utc_month_only(self):
        self.write_ledger([{'kind': 'call', 'cost_usd': 5.0, 'ts': '2026-10-31T23:59:59Z'},
                           {'kind': 'call', 'cost_usd': 2.5, 'ts': '2026-11-01T00:00:00Z'},
                           {'kind': 'reserve', 'batch': 'x', 'usd': 1.0, 'ts': '2026-11-02T00:00:00Z'},
                           {'kind': 'release', 'batch': 'x', 'usd': 1.0, 'ts': '2026-11-02T01:00:00Z'},
                           {'kind': 'call', 'cost_usd': 0.5, 'ts': '2026-11-10T00:00:00Z'}])
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-11'), 3.0)
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-10'), 5.0)
        self.assertEqual(nightly.month_spent(str(self.ledger) + '.none', '2026-11'), 0.0)

    def test_month_total_nets_release_against_original_reserve_month(self):
        self.write_ledger([{'kind': 'reserve', 'batch': 'a', 'usd': 4.0, 'ts': '2026-10-31T23:00:00Z'},
                           {'kind': 'release', 'batch': 'a', 'usd': 4.0, 'ts': '2026-11-01T00:30:00Z'},
                           {'kind': 'call', 'cost_usd': 3.0, 'ts': '2026-11-01T00:20:00Z'}])
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-11'), 3.0)   # 解放が当月をマイナスにしない
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-10'), 0.0)

    def test_open_reservation_of_previous_month_not_mixed_into_current(self):
        self.write_ledger([{'kind': 'reserve', 'batch': 'a', 'usd': 4.0, 'ts': '2026-10-31T23:00:00Z'},
                           {'kind': 'call', 'cost_usd': 1.0, 'ts': '2026-11-02T00:00:00Z'}])
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-11'), 1.0)
        self.assertAlmostEqual(nightly.month_spent(str(self.ledger), '2026-10'), 4.0)

    def test_guard_skips_llm_stage(self):
        self.seed()
        self.write_ledger([{'kind': 'call', 'cost_usd': 99.5, 'ts': '2026-11-03T00:00:00Z'}])
        self.assertEqual(nightly.main(['--date', '2026-11-18', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger), '--budget-month', '100']), 0)
        st = self.status()
        llm = next(s for s in st['stages'] if s['name'] == 'llm')
        self.assertEqual(llm['status'], 'skipped')
        self.assertTrue(any('予算ガード' in g for g in st['guards']))
        self.assertFalse(any('llm/pipeline.py' in ' '.join(c) and '--dry-run' not in c for c in self.calls))

    def test_llm_runs_within_budget_and_key_value_not_in_status(self):
        self.seed()
        nightly.main(['--date', '2026-11-18', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)])
        self.assertEqual(next(s for s in self.status()['stages'] if s['name'] == 'llm')['status'], 'ok')
        self.assertNotIn('dummy', (self.var / 'nightly' / '2026-11-18' / 'status.json').read_text())

    def test_missing_key_fails_llm_stage(self):
        self.seed()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANTHROPIC_API_KEY')
            rc = nightly.main(['--date', '2026-11-18', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)])
        self.assertEqual(rc, 1)
        names = {s['name']: s['status'] for s in self.status()['stages']}
        self.assertEqual(names['llm'], 'failed')
        self.assertEqual(names['import'], 'skipped')


class Run(Env):
    def run_night(self, *extra, date='2026-11-18'):
        return nightly.main(['--date', date, '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger), *extra])

    def test_stage_order_and_no_wrangler_without_apply_prod(self):
        self.seed()
        self.assertEqual(self.run_night(), 0)
        self.assertEqual(self.wrangler_calls(), [])
        st = self.status()
        self.assertEqual([s['name'] for s in st['stages']], nightly.STAGE_NAMES)
        self.assertEqual(next(s for s in st['stages'] if s['name'] == 'prod')['status'], 'skipped')
        self.assertTrue((self.var / 'nightly' / '2026-11-18' / 'approved-sites.sql').exists())
        self.assertTrue((self.var / 'nightly' / '2026-11-18' / 'report.md').exists())

    def test_status_schema(self):
        self.seed()
        self.run_night()
        st = self.status()
        for k in ('schema', 'date', 'started_at', 'finished_at', 'mode', 'ok', 'stages', 'candidates_tonight', 'cost_usd', 'month_spent_usd',
                  'budget_month_usd', 'added', 'rejected_by_reason', 'held', 'guards', 'next_action', 'prod_applied', 'bookmark'):
            self.assertIn(k, st)
        for s in st['stages']:
            self.assertEqual({'name', 'status', 'seconds', 'count', 'note'} <= set(s), True)
            self.assertIn(s['status'], ('ok', 'failed', 'skipped', 'partial'))
        self.assertEqual(st['added'], 2)
        self.assertEqual(st['candidates_tonight'], 2)

    def test_idempotent_second_run_calls_nothing(self):
        self.seed()
        self.run_night()
        n = len(self.calls)
        self.assertEqual(self.run_night(), 0)
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self.status()['added'], 2)

    def test_integrity_violation_stops_before_prod(self):
        self.seed()
        self.violations = {'orphan': [('x',)]}
        self.assertEqual(self.run_night('--apply-prod'), 1)
        st = self.status()
        names = {s['name']: s['status'] for s in st['stages']}
        self.assertEqual(names['gate'], 'failed')
        self.assertEqual(names['prod'], 'skipped')
        self.assertEqual(self.wrangler_calls(), [])
        self.assertTrue(any('整合' in g for g in st['guards']))

    def test_failed_stage_stops_and_resumes_from_it(self):
        self.seed()
        orig = self.fake

        def flaky(cmd, env=None, timeout=None):
            if 'triage' in cmd and not getattr(self, 'recovered', False):
                self.calls.append(cmd)
                return 1, 'boom'
            return orig(cmd, env, timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=flaky):
            self.assertEqual(self.run_night(), 1)
            names = {s['name']: s['status'] for s in self.status()['stages']}
            self.assertEqual((names['discover'], names['triage'], names['fetch']), ('ok', 'failed', 'skipped'))
            self.recovered = True
            n_disc = sum('discover-links' in c for c in self.calls)
            self.assertEqual(self.run_night(), 0)
            self.assertEqual(sum('discover-links' in c for c in self.calls), n_disc)   # 成功済みの段は再実行しない

    def test_zero_approved_makes_no_sql(self):
        add_site(self.db, 'a.jp')
        self.exported = 0
        self.assertEqual(self.run_night(), 0)
        sql = next(s for s in self.status()['stages'] if s['name'] == 'sql')
        self.assertEqual(sql['status'], 'skipped')
        self.assertFalse((self.var / 'nightly' / '2026-11-18' / 'approved-sites.sql').exists())

    def test_max_add_caps_export_copy(self):
        self.seed()
        self.assertEqual(self.run_night('--max-add', '2'), 0)
        st = self.status()
        self.assertTrue(any('1夜の追加上限' in g for g in st['guards']))
        self.assertEqual((self.var / 'nightly' / '2026-11-18' / 'keys.txt').read_text().split(), ['c.jp', 'd.jp'])
        import sqlite3
        con = sqlite3.connect(self.var / 'nightly' / '2026-11-18' / 'export.sqlite')
        self.assertEqual(con.execute("SELECT count(*) FROM llm_judgement WHERE review_state='approved'").fetchone()[0], 2)
        con.close()
        real = sqlite3.connect(self.db_path)   # 本物の台帳は変えない
        self.assertEqual(real.execute("SELECT count(*) FROM llm_judgement WHERE review_state='approved'").fetchone()[0], 3)
        real.close()

    def test_default_has_no_add_cap(self):
        # 既定は上限なし(--max-add 0)。承認済み未出荷はすべて出荷用コピーに残る
        self.seed()
        self.assertEqual(self.run_night(), 0)
        st = self.status()
        self.assertFalse(any('1夜の追加上限' in g for g in st['guards']))
        import sqlite3
        con = sqlite3.connect(self.var / 'nightly' / '2026-11-18' / 'export.sqlite')
        self.assertEqual(con.execute("SELECT count(*) FROM llm_judgement WHERE review_state='approved'").fetchone()[0], 3)
        con.close()

    def test_unexpected_exception_is_recorded_as_failed_stage(self):
        self.seed()
        with mock.patch.object(nightly, 'unjudged_candidates', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_night(), 1)
        st = self.status()
        cand = next(s for s in st['stages'] if s['name'] == 'candidates')
        self.assertEqual(cand['status'], 'failed')
        self.assertIn('boom', cand['note'])
        self.assertIn('失敗あり', (self.var / 'nightly' / '2026-11-18' / 'report.md').read_text())

    def test_corrupt_ledger_is_recorded_not_traceback(self):
        self.seed()
        self.ledger.write_text('not json\n')
        self.assertEqual(self.run_night(), 1)
        st = self.status()
        self.assertFalse(st['ok'])
        self.assertEqual(next(s for s in st['stages'] if s['name'] == 'llm')['status'], 'failed')
        self.assertTrue((self.var / 'nightly' / '2026-11-18' / 'report.md').exists())

    def test_llm_exit2_is_partial_and_resumes_on_rerun(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        st = self.status()
        names = {s['name']: s['status'] for s in st['stages']}
        self.assertEqual(names['llm'], 'partial')
        self.assertEqual(names['prod'], 'skipped')          # 未解決の夜は本番に触れない
        self.assertEqual(self.wrangler_calls(), [])
        self.assertIn('続きから', st['next_action'])
        n_llm = sum('llm/pipeline.py' in ' '.join(c) and '--dry-run' not in c for c in self.calls)
        self.llm_rc = 0
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(sum('llm/pipeline.py' in ' '.join(c) and '--dry-run' not in c for c in self.calls), n_llm + 1)   # llm 段が再実行された
        st = self.status()
        names = {s['name']: s['status'] for s in st['stages']}
        self.assertEqual((names['llm'], names['prod']), ('ok', 'ok'))
        self.assertEqual(sum('import_results' in ' '.join(c) for c in self.calls), 2)   # 後段もやり直された

    def test_candidates_not_rebuilt_after_llm_ok(self):
        self.seed()
        self.run_night()
        cj = (self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').read_text()
        self.assertEqual(self.run_night(), 0)
        self.assertEqual((self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').read_text(), cj)
        self.assertEqual(self.status()['candidates_tonight'], 2)

    def test_from_stage_reruns_without_deleting_status(self):
        self.seed()
        self.run_night()
        n_sql = sum('export_approved_sql' in ' '.join(c) for c in self.calls)
        n_disc = sum('discover-links' in c for c in self.calls)
        self.assertEqual(self.run_night('--from-stage', 'sql'), 0)
        self.assertEqual(sum('export_approved_sql' in ' '.join(c) for c in self.calls), n_sql + 1)
        self.assertEqual(sum('discover-links' in c for c in self.calls), n_disc)
        with self.assertRaises(SystemExit):
            self.args('--from-stage', 'nope')

    def test_only_shipped_keys_recorded_when_export_skips(self):
        self.seed()
        self.exported = 1
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual((self.var / 'nightly' / 'shipped.txt').read_text().split(), ['c.jp'])

    def test_pending_batch_ignores_lock_files(self):
        self.seed()
        d = self.var / 'llm-batches'
        d.mkdir(parents=True)
        (d / 'pipeline-triage.lock').write_text('')
        n = nightly.Night(self.args())
        self.assertFalse(n.pending_batch())
        (d / 'pipeline-triage-run1.json').write_text(json.dumps({'settled': False, 'batch_id': 'b', 'chunks': [[{'id': 'zz_jp'}]]}))
        self.assertTrue(n.pending_batch())
        (d / 'pipeline-triage-run1.json').write_text(json.dumps({'settled': True, 'chunks': []}))
        self.assertFalse(n.pending_batch())

    def test_unsettled_batch_candidates_forced_into_tonights_input(self):
        for i in range(5):
            add_site(self.db, f's{i}.jp')
        d = self.var / 'llm-batches'
        d.mkdir(parents=True)
        (d / 'pipeline-triage-run1.json').write_text(json.dumps({'settled': False, 'batch_id': 'b', 'chunks': [[{'id': 's4_jp'}, {'id': 'gone_jp'}]]}))
        self.assertEqual(self.run_night('--judge-limit', '2'), 0)
        ids = [json.loads(x)['id'] for x in (self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').read_text().splitlines()]
        self.assertEqual(ids, ['s4_jp', 's0_jp'])   # 未精算分は上限の外でも必ず含む
        self.assertTrue(any('未精算' in g for g in self.status()['guards']))

    def test_gate_receives_current_db(self):
        self.seed()
        cur = Path(self.tmp.name) / 'prod.sqlite'
        cur.write_bytes(b'')
        seen = []
        with mock.patch.object(integrity, 'gate', side_effect=lambda sql, current=None: seen.append(current) or {}):
            self.assertEqual(self.run_night('--current-db', str(cur)), 0)
        self.assertEqual(seen, [str(cur)])
        self.assertEqual(self.run_night('--current-db', str(cur) + '.none', '--from-stage', 'gate'), 1)

    def test_lock_prevents_double_run(self):
        import fcntl
        (self.var / 'nightly').mkdir(parents=True)
        with open(self.var / 'nightly' / '.lock', 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_night(), 75)
        self.assertEqual(self.calls, [])


class LockAcrossProcesses(Env):
    """cron は外側の flock なしで nightly.py を直接起動する。別プロセスがロックを持つ間は 75、居なければ通る。"""

    def test_second_process_exits_75_and_single_passes(self):
        import subprocess
        (self.var / 'nightly').mkdir(parents=True)
        code = ("import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.read()")
        holder = subprocess.Popen([sys.executable, '-c', code, str(self.var / 'nightly' / '.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'ready')
            self.assertEqual(nightly.main(['--date', '2026-11-18', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)]), 75)
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
        self.seed()
        self.assertEqual(nightly.main(['--date', '2026-11-18', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)]), 0)

    def test_cron_example_has_no_outer_flock(self):
        doc = (ROOT / 'docs' / 'operations' / 'nightly.md').read_text()
        cron = [ln for ln in doc.splitlines() if 'scripts/nightly.py' in ln and ln.startswith('10 3')]
        self.assertEqual(len(cron), 1)
        self.assertNotIn('flock', cron[0])


class Prod(Env):
    def run_night(self, *extra, date='2026-11-18'):
        return nightly.main(['--date', date, '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger), *extra])

    def test_apply_prod_sequence(self):
        self.seed()
        self.assertEqual(self.run_night('--apply-prod'), 0)
        w = [' '.join(c) for c in self.wrangler_calls()]
        self.assertEqual(len(w), 4)
        self.assertIn('check-data-integrity.py', w[0])      # 投入前の読み取りチェック
        self.assertIn('time-travel info mise', w[1])
        self.assertIn('d1 execute mise --remote', w[2])
        self.assertIn('check-data-integrity.py', w[3])
        st = self.status()
        self.assertTrue(st['prod_applied'])
        self.assertTrue(st['bookmark'].startswith('00000085'))
        shipped = (self.var / 'nightly' / 'shipped.txt').read_text().split()
        self.assertEqual(shipped, ['c.jp', 'd.jp'])   # export が実際に出荷した key だけ(3 件承認のうち 2 件)
        self.assertEqual((self.var / 'nightly' / '2026-11-18' / 'keys.txt').read_text().split(), ['c.jp', 'd.jp'])

    def test_remote_integrity_error_fails_and_keeps_bookmark(self):
        self.seed()
        self.integrity_rc = 1
        self.assertEqual(self.run_night('--apply-prod'), 1)
        st = self.status()
        self.assertFalse(st['ok'])
        self.assertTrue(st['prod_applied'])
        self.assertTrue(st['bookmark'].startswith('00000085'))
        self.assertIn('bookmark', next(s for s in st['stages'] if s['name'] == 'prod')['note'])

    def test_previous_night_integrity_error_blocks_apply(self):
        self.seed()
        self.integrity_before_rc = 1
        self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertFalse(any('execute' in c or 'time-travel' in c for c in self.calls))
        self.assertFalse(self.status()['prod_applied'])
        self.assertFalse((self.var / 'nightly' / 'shipped.txt').exists())

    def test_rerun_after_prod_failure_keeps_bookmark_and_refuses_reapply(self):
        self.seed()
        self.integrity_rc = 1
        self.run_night('--apply-prod')
        bm = (self.var / 'nightly' / '2026-11-18' / 'bookmark.txt').read_text()
        st1 = self.status()['bookmark']
        n_exec = sum('execute' in c for c in self.calls)
        self.integrity_rc = 0
        self.assertEqual(self.run_night('--apply-prod'), 1)    # 再投入は拒否
        self.assertEqual((self.var / 'nightly' / '2026-11-18' / 'bookmark.txt').read_text(), bm)
        self.assertEqual(self.status()['bookmark'], st1)
        self.assertEqual(sum('execute' in c for c in self.calls), n_exec)
        self.assertIn('nightly.md', next(s for s in self.status()['stages'] if s['name'] == 'prod')['note'])

    def test_retry_after_execute_failure_keeps_first_bookmark(self):
        self.seed()
        orig = self.fake
        state = {'fail': True}

        def flaky(cmd, env=None, timeout=None):
            if 'execute' in cmd and state['fail']:
                self.calls.append(cmd)
                return 1, 'boom'
            return orig(cmd, env, timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=flaky):
            self.assertEqual(self.run_night('--apply-prod'), 1)
            bm = (self.var / 'nightly' / '2026-11-18' / 'bookmark.txt').read_text()
            state['fail'] = False
            self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual((self.var / 'nightly' / '2026-11-18' / 'bookmark.txt').read_text(), bm)
        self.assertEqual(sum('time-travel' in c for c in self.calls), 1)

    def test_no_bookmark_no_execute(self):
        self.seed()
        orig = self.fake

        def nobm(cmd, env=None, timeout=None):
            if 'time-travel' in cmd:
                self.calls.append(cmd)
                return 1, 'auth error'
            return orig(cmd, env, timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=nobm):
            self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertFalse(any('execute' in c for c in self.calls))

    def test_anomaly_blocks_prod(self):
        self.seed()
        for i, n in enumerate([10, 10, 10]):
            d = self.var / 'nightly' / f'2026-11-1{i}'
            d.mkdir(parents=True)
            (d / 'status.json').write_text(json.dumps({'added': n}))
        self.assertTrue(nightly.is_anomalous(60, [10, 10, 10]))
        self.assertFalse(nightly.is_anomalous(40, [1, 1, 1]))      # 小さい数は比べない
        self.assertFalse(nightly.is_anomalous(100, [10, 10]))      # 履歴が足りない
        self.exported = 60
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(self.wrangler_calls(), [])
        self.assertTrue(any('前日比の異常' in g for g in self.status()['guards']))


if __name__ == '__main__':
    unittest.main()
