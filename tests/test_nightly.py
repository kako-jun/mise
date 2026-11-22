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
import usage_guard

REAL_LEAF_ORDER = nightly.wikidata_leaf_order
import discover_wikidata
nightly_dw = lambda: discover_wikidata

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
        self.llm_seq = []              # 先頭から 1 回ずつ消費する llm 段の exit code(尽きたら llm_rc)
        self.llm_fail_ids = None       # None: results は空(全件未解決) / 集合: その id 以外は出力ありとして results に書く
        self.exported = 2
        self.p_run = mock.patch.object(nightly, 'run_cmd', side_effect=self.fake)
        self.p_run.start()
        self.addCleanup(self.p_run.stop)
        self.violations = {}
        self.p_leaf = mock.patch.object(nightly, 'wikidata_leaf_order', side_effect=lambda n, **kw: (['shrines', 'temples', 'museums'][:n], 'テスト'))   # 本番 API に出ない
        self.p_leaf.start()
        self.addCleanup(self.p_leaf.stop)
        self.p_gate = mock.patch.object(integrity, 'gate', side_effect=lambda sql, current=None: self.violations)
        self.p_gate.start()
        self.addCleanup(self.p_gate.stop)
        # 使用量ガード(#157): 実機の ~/.config や Cloudflare API を見ない。既定はトークン無し(未設定の警告のみ)
        self.usage_env = Path(self.tmp.name) / 'no-such-usage.env'
        self.p_usage = mock.patch.object(usage_guard, 'DEFAULT_ENV', str(self.usage_env))
        self.p_usage.start()
        self.addCleanup(self.p_usage.stop)
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
            if self.llm_fail_ids is None:
                results = []
            else:
                ids = [json.loads(l)['id'] for l in Path(cmd[2]).read_text().splitlines() if l.strip()]
                results = [{'id': i, 'output': {'x': 1}} for i in ids if i not in self.llm_fail_ids]
            Path(cmd[cmd.index('--out') + 1]).write_text(json.dumps({'results': results}))
            return (self.llm_seq.pop(0) if self.llm_seq else self.llm_rc), ''
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

    def test_dry_run_shows_llm_retries(self):
        for extra, want in (((), '未解決の再試行 2 回'), (('--llm-retries', '0'), '未解決の再試行 0 回')):
            with mock.patch('builtins.print') as pr:
                nightly.main(['--dry-run', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger), *extra])
            self.assertIn(want, pr.call_args[0][0])

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

    def test_favicon_stage_disabled_by_default(self):
        self.seed()
        self.run_night()
        fav = next(s for s in self.status()['stages'] if s['name'] == 'favicon')
        self.assertEqual(fav['status'], 'skipped')
        self.assertFalse(any('favicon.py' in ' '.join(c) for c in self.calls))

    def test_favicon_stage_enabled_runs_script_and_never_touches_prod(self):
        self.seed()
        self.assertEqual(self.run_night('--favicon-limit', '5'), 0)
        calls = [c for c in self.calls if 'favicon.py' in ' '.join(c)]
        joined = [' '.join(c) for c in calls]
        self.assertTrue(all(j.startswith('uv run --with pillow python ') for j in joined))   # 夜間本体は Pillow を持たない
        self.assertTrue(any(' run --apply --limit 5 ' in j for j in joined))
        self.assertFalse(any(' sql ' in j for j in joined))   # 夜間は SQL を作らない（ローカルにしか置かない画像を D1 に指す SQL の誤適用を避ける）
        self.assertEqual(self.wrangler_calls(), [])

    def test_favicon_failure_does_not_fail_the_night(self):
        self.seed()
        orig = self.fake
        def failing(cmd, env=None, timeout=None):
            if 'favicon.py' in ' '.join(cmd):
                self.calls.append(cmd); return 1, 'boom'
            return orig(cmd, env, timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=failing):
            self.assertEqual(self.run_night('--favicon-limit', '5'), 0)
        self.assertTrue(any('favicon' in g for g in self.status()['guards']))

    def test_usage_stage_without_token_warns_but_does_not_fail(self):
        self.seed()
        self.assertEqual(self.run_night(), 0)
        st = self.status()
        u = next(s for s in st['stages'] if s['name'] == 'usage')
        self.assertEqual(u['status'], 'ok')
        self.assertTrue(any('使用量ガードは動いていません' in g and 'docs/operations/usage-guard.md' in g for g in st['guards']))

    def test_usage_stage_runs_even_after_a_failed_stage_and_never_fails_the_night(self):
        self.seed()
        orig = self.fake
        def fake(cmd, env=None, timeout=None):
            if 'run.py' in ' '.join(cmd) and 'triage' in cmd:
                return 1, 'boom'
            return orig(cmd, env=env, timeout=timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=fake), mock.patch.object(usage_guard, 'run', side_effect=RuntimeError('x')) as ug:
            self.run_night()
        ug.assert_called_once()
        st = self.status()
        u = next(s for s in st['stages'] if s['name'] == 'usage')
        self.assertEqual(u['status'], 'ok')       # 使用量ガードの例外は段の失敗にしない
        self.assertEqual(next(s for s in st['stages'] if s['name'] == 'triage')['status'], 'failed')

    def test_usage_guard_warnings_go_to_guards_and_report(self):
        self.seed()
        with mock.patch.object(usage_guard, 'run', return_value={'guards': ['使用量: D1 読み取り行 が当月の枠の 55.0%'], 'note': 'x'}):
            self.assertEqual(self.run_night(), 0)
        self.assertIn('使用量: D1 読み取り行 が当月の枠の 55.0%', self.status()['guards'])
        self.assertIn('55.0%', (self.var / 'nightly' / '2026-11-18' / 'report.md').read_text())

    def test_usage_guard_gets_current_utc_not_the_night_name(self):
        # 再開・再実行で夜の名前が過去の日付でも、「今日/昨日」の基準は現在の UTC(date=None)。夜の名前を渡すと急増判定・月末予測が狂う
        self.seed()
        with mock.patch.object(usage_guard, 'run', return_value={'guards': [], 'note': 'x'}) as ug:
            self.assertEqual(self.run_night(date='2026-01-05'), 0)
        self.assertIsNone(ug.call_args[0][0])

    def test_no_usage_guard_option_skips_the_stage(self):
        self.seed()
        with mock.patch.object(usage_guard, 'run') as ug:
            self.assertEqual(self.run_night('--no-usage-guard'), 0)
        ug.assert_not_called()
        self.assertEqual(next(s for s in self.status()['stages'] if s['name'] == 'usage')['status'], 'skipped')

    def test_dry_run_mentions_usage_stage(self):
        with mock.patch('builtins.print') as pr:
            nightly.main(['--dry-run', '--db', str(self.db_path), '--var-dir', str(self.var), '--ledger', str(self.ledger)])
        self.assertIn('14 usage', pr.call_args[0][0])
        self.assertIn('トークン未設定', pr.call_args[0][0])

    def test_status_schema(self):
        self.seed()
        self.run_night()
        st = self.status()
        for k in ('schema', 'date', 'started_at', 'finished_at', 'mode', 'ok', 'stages', 'candidates_tonight', 'cost_usd', 'month_spent_usd',
                  'budget_month_usd', 'added', 'rejected_by_reason', 'held', 'guards', 'next_action', 'prod_applied', 'bookmark'):
            self.assertIn(k, st)
        for s in st['stages']:
            self.assertEqual({'name', 'status', 'seconds', 'count', 'note'} <= set(s), True)
            self.assertIn(s['status'], ('ok', 'failed', 'skipped', 'partial', 'warning'))
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

    def llm_runs(self):
        return sum('llm/pipeline.py' in ' '.join(c) and '--dry-run' not in c for c in self.calls)

    def test_partial_resume_keeps_original_candidate_set_and_denominator(self):
        self.seed_many(9)
        self.llm_rc = 2
        self.llm_fail_ids = {'h00_jp', 'h01_jp', 'h02_jp'}
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(next(s['status'] for s in self.status()['stages'] if s['name'] == 'llm'), 'partial')
        path = self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl'
        original = path.read_bytes()
        for i in range(3, 9):
            self.db.execute(f"INSERT INTO llm_judgement ({JCOLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f'h{i:02d}.jp', 'h', 'pipeline', 'm', 'x', 'x', 'pass', 'none', '', '["astronomy"]', '[]', 'no', 'individual', '', 'standalone', 'high', 'approved'))
        self.db.commit()
        self.llm_fail_ids = {'h00_jp', 'h01_jp'}
        self.assertEqual(self.run_night('--apply-prod'), 0)
        st = self.status()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(st['candidates_tonight'], 9)
        self.assertEqual(next(s['status'] for s in st['stages'] if s['name'] == 'llm'), 'ok')
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE reject_reason='llm_unresolved'").fetchone()[0], 2)

    def test_missing_candidates_on_resume_stops_before_llm_or_prod(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        (self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').unlink()
        n_llm = self.llm_runs()
        self.assertEqual(self.run_night('--apply-prod'), 1)
        st = self.status()
        self.assertEqual(next(s['status'] for s in st['stages'] if s['name'] == 'candidates'), 'failed')
        self.assertEqual(self.llm_runs(), n_llm)
        self.assertEqual(self.wrangler_calls(), [])
        # 通常再実行を繰り返しても元の入力を勝手に作り直さない。
        self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertEqual(next(s['status'] for s in self.status()['stages'] if s['name'] == 'candidates'), 'failed')
        self.assertFalse((self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').exists())
        self.assertEqual(self.llm_runs(), n_llm)
        # 先行段が失敗して candidates 段が skipped になっても、停止状態は消えない。
        with mock.patch.object(nightly.Night, 's_discover', side_effect=nightly.StageFailed('discover failure')):
            self.assertEqual(self.run_night('--apply-prod', '--from-stage', 'discover'), 1)
        self.assertTrue(self.status()['candidate_rebuild_required'])
        self.assertEqual(next(s['status'] for s in self.status()['stages'] if s['name'] == 'candidates'), 'skipped')
        self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertTrue(self.status()['candidate_rebuild_required'])
        self.assertEqual(self.run_night('--apply-prod', '--from-stage', 'fetch'), 1)
        self.assertTrue(self.status()['candidate_rebuild_required'])
        self.assertFalse((self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').exists())
        # 明示的な再生成のみ許可。
        self.assertEqual(self.run_night('--apply-prod', '--from-stage', 'candidates'), 0)
        self.assertTrue((self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl').exists())
        self.assertFalse(self.status()['candidate_rebuild_required'])

    def test_missing_or_inconsistent_candidate_counts_stop_on_resume(self):
        for changed in ('missing_top', 'missing_stage', 'mismatch'):
            with self.subTest(changed=changed):
                date = {'missing_top': '2026-11-18', 'missing_stage': '2026-11-19', 'mismatch': '2026-11-20'}[changed]
                self.llm_rc = 2
                self.assertEqual(self.run_night('--apply-prod', date=date), 0)
                path = self.var / 'nightly' / date / 'status.json'
                st = json.loads(path.read_text())
                candidates = next(s for s in st['stages'] if s['name'] == 'candidates')
                if changed == 'missing_top':
                    st.pop('candidates_tonight')
                elif changed == 'missing_stage':
                    candidates['count'].pop('candidates')
                else:
                    st['candidates_tonight'] += 1
                path.write_text(json.dumps(st))
                self.assertEqual(self.run_night('--apply-prod', date=date), 1)
                self.assertEqual(next(s['status'] for s in self.status(date)['stages'] if s['name'] == 'candidates'), 'failed')

    def test_force_fetch_checks_existing_candidate_input_before_rebuild(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        path = self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl'
        path.unlink()
        llm_calls = self.llm_runs()
        self.assertEqual(self.run_night('--apply-prod', '--from-stage', 'fetch'), 1)
        self.assertFalse(path.exists())
        self.assertTrue(self.status()['candidate_rebuild_required'])
        self.assertEqual(self.llm_runs(), llm_calls)
        self.assertEqual(self.wrangler_calls(), [])

    def test_same_count_changed_candidate_input_stops_before_llm(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        path = self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['id'] = 'different_id'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        n_llm = self.llm_runs()
        self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertEqual(self.llm_runs(), n_llm)
        self.assertTrue(self.status()['candidate_rebuild_required'])
        self.assertEqual(self.wrangler_calls(), [])

    def test_legacy_checkpoint_without_digest_is_upgraded_after_validation(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        path = self.var / 'nightly' / '2026-11-18' / 'status.json'
        st = json.loads(path.read_text())
        st.pop('candidate_checkpoint')
        path.write_text(json.dumps(st))
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(len(self.status()['candidate_checkpoint']['sha256']), 64)

    def test_candidate_checkpoint_survives_prior_stage_failure_before_validation(self):
        self.seed()
        self.llm_rc = 2
        self.assertEqual(self.run_night('--apply-prod'), 0)
        path = self.var / 'nightly' / '2026-11-18' / 'candidates.jsonl'
        path.unlink()
        with mock.patch.object(nightly.Night, 's_discover', side_effect=nightly.StageFailed('discover failure')):
            self.assertEqual(self.run_night('--apply-prod', '--from-stage', 'discover'), 1)
        self.assertIsNotNone(self.status()['candidate_checkpoint'])
        self.assertEqual(self.run_night('--apply-prod'), 1)
        self.assertFalse(path.exists())
        self.assertTrue(self.status()['candidate_rebuild_required'])

    def seed_many(self, n=30):
        for i in range(n):
            add_site(self.db, f'h{i:02d}.jp')

    def frontier_row(self, key):
        return self.db.execute('SELECT state, reject_reason FROM frontier WHERE key=?', (key,)).fetchone()

    def test_llm_retry_resolves_in_same_run(self):
        self.seed_many()
        self.llm_seq = [2, 0]
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(self.llm_runs(), 2)
        st = self.status()
        llm = next(s for s in st['stages'] if s['name'] == 'llm')
        self.assertEqual((llm['status'], llm['count']['llm_retries']), ('ok', 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE state='rejected'").fetchone()[0], 0)

    def test_llm_unresolved_after_retries_is_cut_off_and_night_proceeds(self):
        self.seed_many()
        self.llm_rc = 2
        self.llm_fail_ids = {'h07_jp'}
        self.assertEqual(self.run_night('--apply-prod'), 0)
        self.assertEqual(self.llm_runs(), 3)    # 初回 + 再試行 2 回
        self.assertEqual(tuple(self.frontier_row('h07.jp')), ('rejected', 'llm_unresolved'))
        self.assertEqual(self.frontier_row('h08.jp')['state'], 'candidate')
        st = self.status()
        names = {s['name']: s['status'] for s in st['stages']}
        self.assertEqual((names['llm'], names['import'], names['approve']), ('ok', 'ok', 'ok'))    # 先へ進む(承認 0 件なので SQL・prod は対象無しで skipped)
        self.assertFalse(any(s['status'] == 'partial' for s in st['stages']))
        llm = next(s for s in st['stages'] if s['name'] == 'llm')
        self.assertIn('1 件', llm['note'])
        self.assertTrue(any('LLM 未解決 1 件を打ち切り' in g and 'h07.jp' in g for g in st['guards']))

    def test_cut_off_above_threshold_stays_partial(self):
        self.seed_many()
        self.llm_rc = 2
        self.llm_fail_ids = {'h01_jp', 'h02_jp'}     # 2/30 = 6.7% > 5%
        self.assertEqual(self.run_night('--apply-prod'), 0)
        st = self.status()
        names = {s['name']: s['status'] for s in st['stages']}
        self.assertEqual((names['llm'], names['prod']), ('partial', 'skipped'))
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE reject_reason='llm_unresolved'").fetchone()[0], 0)
        self.assertTrue(any('打ち切らず止めた' in g for g in st['guards']))
        self.assertEqual(self.wrangler_calls(), [])

    def test_small_batch_two_unresolved_can_continue(self):
        self.seed_many(9)
        self.llm_rc = 2
        self.llm_fail_ids = {'h01_jp', 'h02_jp'}
        self.assertEqual(self.run_night('--apply-prod'), 0)
        st = self.status()
        self.assertEqual(next(s['status'] for s in st['stages'] if s['name'] == 'llm'), 'ok')
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE reject_reason='llm_unresolved'").fetchone()[0], 2)

    def test_small_batch_systemic_failure_stays_partial(self):
        self.seed_many(4)
        self.llm_rc = 2
        self.llm_fail_ids = {'h01_jp', 'h02_jp'}
        self.assertEqual(self.run_night('--apply-prod'), 0)
        st = self.status()
        self.assertEqual(next(s['status'] for s in st['stages'] if s['name'] == 'llm'), 'partial')
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE reject_reason='llm_unresolved'").fetchone()[0], 0)

    def test_cut_off_above_count_limit_stays_partial(self):
        self.seed_many(500)
        self.llm_rc = 2
        self.llm_fail_ids = {f'h{i:02d}_jp' for i in range(21)}   # 21/500 = 4.2% だが 20 件超
        with mock.patch.object(nightly, 'LLM_CUTOFF_MAX_COUNT', 20):
            self.assertEqual(self.run_night('--judge-limit', '500'), 0)
        names = {s['name']: s['status'] for s in self.status()['stages']}
        self.assertEqual(names['llm'], 'partial')
        self.assertEqual(self.db.execute("SELECT count(*) FROM frontier WHERE reject_reason='llm_unresolved'").fetchone()[0], 0)

    def test_llm_retries_zero_keeps_old_behaviour(self):
        self.seed_many()
        self.llm_rc = 2
        self.llm_fail_ids = {'h07_jp'}
        self.assertEqual(self.run_night('--apply-prod', '--llm-retries', '0'), 0)
        self.assertEqual(self.llm_runs(), 1)
        names = {s['name']: s['status'] for s in self.status()['stages']}
        self.assertEqual((names['llm'], names['prod']), ('partial', 'skipped'))
        self.assertEqual(self.frontier_row('h07.jp')['state'], 'candidate')

    def test_negative_llm_retries_rejected(self):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            self.args('--llm-retries', '-1')

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
        import filelock
        (self.var / 'nightly').mkdir(parents=True)
        with open(self.var / 'nightly' / '.lock', 'w') as lf:
            filelock.lock(lf, blocking=False)
            self.assertEqual(self.run_night(), 75)
        self.assertEqual(self.calls, [])


class LockAcrossProcesses(Env):
    """cron は外側の flock なしで nightly.py を直接起動する。別プロセスがロックを持つ間は 75、居なければ通る。"""

    def test_second_process_exits_75_and_single_passes(self):
        import subprocess
        (self.var / 'nightly').mkdir(parents=True)
        code = ("import sys; sys.path.insert(0,sys.argv[2]); import filelock; f=open(sys.argv[1],'w'); filelock.lock(f); print('ready', flush=True); sys.stdin.read()")
        holder = subprocess.Popen([sys.executable, '-c', code, str(self.var / 'nightly' / '.lock'), str(ROOT / 'scripts' / 'llm')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
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

    def test_date_accepts_sequence_suffix_and_rejects_bad_forms(self):
        for ok in ('2026-11-20', '2026-11-20-2', '2026-11-20-10'):
            self.assertEqual(self.args(date=ok).date, ok)
        for bad in ('2026-11-20-1', '2026-11-20-x', '2026-11-20-02', '2026-11-20-0', '2026-11-20-', '2026-11-2'):
            with self.assertRaises(SystemExit, msg=bad), mock.patch('sys.stderr'):
                self.args(date=bad)

    def put_night(self, name, added, applied=False):
        d = self.var / 'nightly' / name
        d.mkdir(parents=True)
        (d / 'status.json').write_text(json.dumps({'added': added, 'prod_applied': applied}))

    def test_history_sums_applied_cycles_per_day_and_ignores_same_day(self):
        nd = self.var / 'nightly'
        for name, n, ap in [('2026-11-10', 10, True), ('2026-11-11', 20, True), ('2026-11-11-2', 30, True), ('2026-11-11-10', 40, True),
                            ('2026-11-12', 5, True), ('2026-11-12-2', 99, True)]:
            self.put_night(name, n, ap)
        (nd / 'junk').mkdir()
        self.assertEqual(nightly.history_added(str(nd), '2026-11-12-2'), [90, 10])
        self.assertEqual(nightly.history_added(str(nd), '2026-11-13'), [104, 90, 10])
        self.assertEqual(nightly.history_added(str(nd), '2026-11-13', n=2), [104, 90])
        self.assertEqual(nightly.history_added(str(nd / 'none'), '2026-11-13'), [])

    def test_history_does_not_double_count_unapplied_cycles(self):
        nd = self.var / 'nightly'
        self.put_night('2026-11-10', 50)          # 未反映 2 回 = 同じ候補の再生成 -> 最大値
        self.put_night('2026-11-10-2', 60)
        self.put_night('2026-11-11', 10, True)    # 反映済みがあればそれだけ(未反映は足さない)
        self.put_night('2026-11-11-2', 70)
        self.assertEqual(nightly.history_added(str(nd), '2026-11-12'), [10, 60])

    def test_anomaly_factor_option(self):
        self.assertEqual(self.args().anomaly_factor, 5)
        self.assertEqual(self.args('--anomaly-factor', '20').anomaly_factor, 20)
        for bad in ('0', '-1'):
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                self.args('--anomaly-factor', bad)
        self.assertTrue(nightly.is_anomalous(60, [10, 10, 10]))
        self.assertFalse(nightly.is_anomalous(60, [10, 10, 10], 10))
        self.assertIn('異常ガードの倍率: 20', nightly.plan(self.args('--anomaly-factor', '20')))

    def test_second_cycle_runs_every_stage_in_own_dir(self):
        self.seed()
        self.assertEqual(self.run_night(date='2026-11-18'), 0)
        self.assertEqual(self.run_night(date='2026-11-18-2'), 0)
        st = self.status('2026-11-18-2')
        self.assertEqual(st['date'], '2026-11-18-2')
        self.assertEqual([s['name'] for s in st['stages']], nightly.STAGE_NAMES)
        skipped_ok = {'prod', 'favicon', 'media'}
        for s in st['stages']:
            self.assertEqual(s['status'], 'skipped' if s['name'] in skipped_ok else 'ok', s['name'])
        self.assertTrue((self.var / 'nightly' / '2026-11-18-2' / 'approved-sites.sql').exists())

    def test_anomaly_guard_with_suffixed_history_blocks(self):
        self.seed()
        for name in ('2026-11-14', '2026-11-15', '2026-11-16', '2026-11-17'):
            self.put_night(name, 10, True)
            self.put_night(name + '-2', 10, True)   # 日合計 20 / 日
        self.put_night('2026-11-18', 5, True)       # 同日の他サイクルは基準に入らない
        self.exported = 120                         # > 5 * 20
        self.assertEqual(self.run_night('--apply-prod', date='2026-11-18-2'), 0)
        self.assertEqual(self.wrangler_calls(), [])
        self.assertTrue(any('前日比の異常' in g for g in self.status('2026-11-18-2')['guards']))


if __name__ == '__main__':
    unittest.main()


class WikidataStage(Env):
    def test_discover_runs_wikidata_with_leaves_and_continues_on_failure(self):
        self.seed()
        a = self.args('--wikidata-leaves', '2')
        n = nightly.Night(a)
        n.s_discover()
        wd = [c for c in self.calls if 'discover-wikidata' in c]
        self.assertEqual(len(wd), 1)
        self.assertEqual(wd[0][wd[0].index('--leaves') + 1], 'shrines,temples')
        self.assertEqual(wd[0][wd[0].index('--limit-leaves') + 1], '2')

    def test_disabled_with_zero(self):
        n = nightly.Night(self.args('--wikidata-leaves', '0'))
        n.s_discover()
        self.assertFalse([c for c in self.calls if 'discover-wikidata' in c])

    def test_leaf_order_prefers_short_leaves_and_falls_back(self):
        def boom():
            raise OSError('offline')
        order, how = REAL_LEAF_ORDER(50, fetch=lambda: [{'id': 'shrines', 'site_count': 150}, {'id': 'temples', 'site_count': 5}, {'id': 'libraries', 'site_count': 0}, {'id': 'museums', 'site_count': 200}])
        self.assertEqual(order[-1], 'temples')                  # 少ない順(API に無い葉は 0 扱いで先)。5 件の temples が最後
        self.assertNotIn('shrines', order); self.assertNotIn('museums', order)   # 100 以上は除外
        order, how = REAL_LEAF_ORDER(2, fetch=boom)
        self.assertEqual(order, ['libraries', 'shrines'])       # 対応表の先頭から(固定順)
        self.assertIn('固定', how)

    def _leaf_order_db(self, done_leaves):
        rows = nightly_dw().load_map()
        for r in rows:
            if r['leaf_id'] in done_leaves:
                frontier.set_cursor(self.db, 'wikidata', nightly_dw().cursor_name(r), 'done')
        self.db.commit()

    def test_leaf_order_skips_done_leaves_and_picks_next(self):
        leaves = list(dict.fromkeys(r['leaf_id'] for r in nightly_dw().load_map()))
        self.assertGreater(len(leaves), 6)
        self._leaf_order_db(set(leaves[:3]))
        order, how = REAL_LEAF_ORDER(3, fetch=lambda: [], db=str(self.db_path))   # 全葉 0 件(不足)なので対応表の順
        self.assertEqual(order, leaves[3:6])

    def test_leaf_order_partial_leaf_stays_and_all_done_is_zero(self):
        rows = nightly_dw().load_map()
        leaves = list(dict.fromkeys(r['leaf_id'] for r in rows))
        multi = next(l for l in leaves if sum(r['leaf_id'] == l for r in rows) > 1)
        frontier.set_cursor(self.db, 'wikidata', nightly_dw().cursor_name(next(r for r in rows if r['leaf_id'] == multi)), 'done')
        self.db.commit()
        self.assertIn(multi, REAL_LEAF_ORDER(50, fetch=lambda: [], db=str(self.db_path))[0])   # 行の一部だけ done なら未完了
        self._leaf_order_db(set(leaves))
        self.assertEqual(REAL_LEAF_ORDER(3, fetch=lambda: [], db=str(self.db_path)), ([], '完了'))
        self.assertEqual(REAL_LEAF_ORDER(3, fetch=lambda: (_ for _ in ()).throw(OSError()), db=str(self.db_path)), ([], '完了'))   # 固定順でも完了は 0 件

    def test_no_short_leaf_means_empty_but_api_failure_falls_back(self):
        rows = nightly_dw().load_map()
        full = [{'id': l, 'site_count': 500} for l in dict.fromkeys(r['leaf_id'] for r in rows)]
        self.assertEqual(REAL_LEAF_ORDER(3, fetch=lambda: full)[0], [])        # 全部 100 件以上 -> スキップ
        def boom():
            raise OSError('offline')
        self.assertEqual(len(REAL_LEAF_ORDER(3, fetch=boom)[0]), 3)            # API 不可のときだけ固定順

    def test_stage_skips_when_no_leaves_and_records_guard(self):
        self.p_leaf.stop()
        with mock.patch.object(nightly, 'wikidata_leaf_order', return_value=([], '完了')):
            n = nightly.Night(self.args('--wikidata-leaves', '2'))
            n.s_discover()
        self.assertFalse([c for c in self.calls if 'discover-wikidata' in c])
        self.assertTrue(any('スキップ' in g for g in n.status['guards']))
        self.p_leaf.start()

    def test_exit_3_is_recorded_as_abort_and_continues(self):
        orig = self.fake
        def fake(cmd, env=None, timeout=None):
            if 'discover-wikidata' in cmd:
                self.calls.append(cmd); return 3, '{"aborted": "WDQS 429 retry_after=60"}'
            return orig(cmd, env, timeout)
        with mock.patch.object(nightly, 'run_cmd', side_effect=fake):
            n = nightly.Night(self.args('--wikidata-leaves', '2'))
            r = n.s_discover()
        self.assertTrue(any('Wikidata が中断' in g and 'Retry-After' in g for g in n.status['guards']))
        self.assertFalse(any('失敗' in g for g in n.status['guards']))
        self.assertIn('中断', r['note'])


class CcSampleEvery(Env):
    def _cc(self):
        return [c for c in self.calls if 'discover-cc' in c]

    def test_default_is_1000_and_passed_to_discover_cc(self):
        self.seed()
        self.assertEqual(self.args().cc_sample_every, 1000)
        nightly.Night(self.args()).s_discover()
        cc = self._cc()
        self.assertEqual(len(cc), 1)
        self.assertEqual(cc[0][cc[0].index('--sample-every') + 1], '1000')

    def test_custom_value_passed_to_discover_cc(self):
        self.seed()
        nightly.Night(self.args('--cc-sample-every', '10')).s_discover()
        cc = self._cc()
        self.assertEqual(cc[0][cc[0].index('--sample-every') + 1], '10')
        self.assertEqual(cc[0][cc[0].index('--limit') + 1], '200')   # 1 夜の上限は従来どおり

    def test_invalid_value_rejected(self):
        for bad in ('0', '-5'):
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                self.args('--cc-sample-every', bad)

    def test_disabled_when_discover_limit_zero(self):
        nightly.Night(self.args('--discover-limit', '0', '--cc-sample-every', '10')).s_discover()
        self.assertFalse(self._cc())

    def test_dry_run_shows_sample_every(self):
        self.assertIn('抽出率 1/100', nightly.plan(self.args('--cc-sample-every', '100')))
        self.assertNotIn('抽出率', nightly.plan(self.args('--discover-limit', '0')))
