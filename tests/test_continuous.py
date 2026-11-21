import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('continuous', ROOT / 'scripts' / 'continuous.py')
continuous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(continuous)


class ContinuousTest(unittest.TestCase):
    def test_cycle_names_reserve_and_never_use_cron_name(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            (directory / '2026-11-21-2').mkdir()
            self.assertEqual(continuous.next_cycle(directory, '2026-11-21'), '2026-11-21-3')
            self.assertEqual(continuous.next_cycle(directory, '2026-11-21'), '2026-11-21-4')

    def test_budget_partial_and_anomaly_stop_even_if_exit_zero(self):
        ok = {'ok': True, 'finished_at': 'yes', 'added': 0, 'candidates_tonight': 0, 'stages': []}
        self.assertIsNone(continuous.outcome(ok, 0))
        budget = {**ok, 'candidates_tonight': 20, 'stages': [{'name': 'llm', 'status': 'skipped'}]}
        self.assertIn('LLM', continuous.outcome(budget, 0))
        partial = {**ok, 'stages': [{'name': 'llm', 'status': 'partial'}]}
        self.assertIsNotNone(continuous.outcome(partial, 0))
        anomaly = {**ok, 'added': 20, 'stages': [{'name': 'prod', 'status': 'skipped', 'note': '異常ガード'}]}
        self.assertIn('反映', continuous.outcome(anomaly, 0))
        self.assertIsNotNone(continuous.outcome(None, 0))

    def test_only_clean_budget_stop_is_automatically_resumable(self):
        budget = {'ok': True, 'finished_at': 'yes', 'added': 0, 'stages': [
            {'name': 'llm', 'status': 'skipped', 'note': '予算ガード: 上限超過'}]}
        self.assertTrue(continuous.budget_paused(budget))
        self.assertFalse(continuous.budget_paused({**budget, 'added': 20, 'prod_applied': False}))
        self.assertFalse(continuous.budget_paused({**budget, 'ok': False}))
        self.assertFalse(continuous.budget_paused({**budget, 'stages': budget['stages'] + [
            {'name': 'media', 'status': 'warning'}]}))

    def test_interrupted_after_gate_never_replays_unknown_prod(self):
        st = {'finished_at': None, 'stages': [{'name': 'gate', 'status': 'ok'}]}
        self.assertTrue(continuous.unsafe_to_resume(st))
        self.assertFalse(continuous.unsafe_to_resume({'finished_at': None, 'stages': [{'name': 'fetch', 'status': 'ok'}]}))
        self.assertFalse(continuous.unsafe_to_resume({'finished_at': 'yes', 'stages': [{'name': 'gate', 'status': 'ok'}]}))

    def test_discovery_guard_records_failure_including_retry_after(self):
        status = {'stages': [{'name': 'discover', 'status': 'ok'}], 'guards': []}
        self.assertTrue(continuous.discovery_succeeded(status))
        for guard in ('discover-cc 失敗(続行)', 'Wikidata が中断した(429/503・Retry-After。続行)',
                      'discover-wikidata スキップ(本番 API が取れない)'):
            self.assertFalse(continuous.discovery_succeeded({**status, 'guards': [guard]}))

    def test_work_ready_uses_same_candidate_identity_as_nightly(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'frontier.sqlite'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE frontier (state TEXT, fetch_attempts INTEGER, next_fetch_at TEXT)')
            with patch.object(continuous.nightly, 'unjudged_candidates', return_value=[]) as pending:
                self.assertFalse(continuous.work_ready(db))
                pending.assert_called_once_with(str(db))

    def test_controller_runs_backlog_without_rediscovering_every_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            var = Path(d)
            folder = var / 'nightly'
            folder.mkdir()
            (folder / 'continuous.json').write_text(json.dumps({'last_discovery': continuous.now().isoformat()}))
            args = continuous.argparse.Namespace(var_dir=d, db=str(var / 'frontier.sqlite'), apply_prod=True,
                fetch_limit=1500, judge_limit=1500, budget_month=100, discover_limit=3000, cc_sample_every=100,
                wikidata_leaves=20, anomaly_factor=5, media_favicon_limit=0, discovery_hours=6,
                wait_seconds=1, idle_seconds=1, once=True)
            def fake_run(cmd, **kwargs):
                self.assertIn('--skip-discovery', cmd)
                self.assertIn('--apply-prod', cmd)
                name = cmd[cmd.index('--date') + 1]
                (folder / name / 'status.json').write_text(json.dumps({'ok': True, 'finished_at': 'yes',
                    'added': 3, 'prod_applied': True, 'candidates_tonight': 3, 'stages': []}))
                return type('Result', (), {'returncode': 0})()
            with patch.object(continuous.subprocess, 'run', side_effect=fake_run):
                self.assertEqual(continuous.run(args), 0)
            state = json.loads((folder / 'continuous.json').read_text())
            self.assertIsNone(state['active'])
            self.assertTrue(state['last_completed'].endswith('-2'))

    def test_budget_waits_this_month_and_restarts_next_month(self):
        import datetime
        with tempfile.TemporaryDirectory() as d:
            var = Path(d)
            folder = var / 'nightly'
            folder.mkdir()
            state_path = folder / 'continuous.json'
            state_path.write_text(json.dumps({'budget_pause_month': '2026-11', 'last_discovery': '2026-11-30T00:00:00+00:00'}))
            args = continuous.argparse.Namespace(var_dir=d, db=str(var / 'frontier.sqlite'), apply_prod=True,
                fetch_limit=1500, judge_limit=1500, budget_month=100, discover_limit=3000, cc_sample_every=100,
                wikidata_leaves=20, anomaly_factor=5, media_favicon_limit=0, discovery_hours=6,
                wait_seconds=1, idle_seconds=1, once=True)
            def fake_run(cmd, **kwargs):
                name = cmd[cmd.index('--date') + 1]
                (folder / name / 'status.json').write_text(json.dumps({'ok': True, 'finished_at': 'yes',
                    'added': 3, 'prod_applied': True, 'candidates_tonight': 3, 'stages': []}))
                return type('Result', (), {'returncode': 0})()
            with patch.object(continuous, 'now', return_value=datetime.datetime(2026, 11, 30, tzinfo=datetime.timezone.utc)), \
                 patch.object(continuous.subprocess, 'run', side_effect=fake_run) as child:
                self.assertEqual(continuous.run(args), 0)
                child.assert_not_called()
            with patch.object(continuous, 'now', return_value=datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc)), \
                 patch.object(continuous.subprocess, 'run', side_effect=fake_run) as child:
                self.assertEqual(continuous.run(args), 0)
                child.assert_called_once()
            self.assertNotIn('budget_pause_month', json.loads(state_path.read_text()))

    def test_budget_wait_keeps_discovery_running_without_api_or_publication(self):
        import datetime
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'nightly'
            folder.mkdir()
            (folder / 'continuous.json').write_text(json.dumps({'budget_pause_month': '2026-11'}))
            args = continuous.argparse.Namespace(var_dir=d, db=str(Path(d) / 'frontier.sqlite'), apply_prod=True,
                fetch_limit=1500, judge_limit=1500, budget_month=100, discover_limit=3000, cc_sample_every=100,
                wikidata_leaves=20, anomaly_factor=5, media_favicon_limit=0, discovery_hours=6,
                wait_seconds=1, idle_seconds=1, once=True)
            def fake_run(cmd, **kwargs):
                self.assertIn('--discover-only', cmd)
                self.assertNotIn('--apply-prod', cmd)
                name = cmd[cmd.index('--date') + 1]
                (folder / name / 'status.json').write_text(json.dumps({'ok': True, 'finished_at': 'yes',
                    'added': 0, 'candidates_tonight': 0, 'stages': [{'name':'discover','status':'ok'}]}))
                return type('Result', (), {'returncode': 0})()
            with patch.object(continuous, 'now', return_value=datetime.datetime(2026, 11, 30, tzinfo=datetime.timezone.utc)), \
                 patch.object(continuous.subprocess, 'run', side_effect=fake_run) as child:
                self.assertEqual(continuous.run(args), 0)
                child.assert_called_once()
            self.assertEqual(json.loads((folder / 'continuous.json').read_text())['budget_pause_month'], '2026-11')

    def test_discover_only_runs_no_fetch_llm_or_prod_stages(self):
        with tempfile.TemporaryDirectory() as d:
            args = continuous.nightly.parse_args(['--date','2026-11-21-2', '--discover-only',
                '--var-dir', d, '--db', str(Path(d) / 'none.sqlite')])
            job = continuous.nightly.Night(args)
            with patch.object(job, 'spent', return_value=0), patch.object(job, 's_discover', return_value={}), \
                 patch.object(job, 's_triage', return_value={}), patch.object(job, 's_llm') as llm, \
                 patch.object(job, 's_prod') as prod:
                self.assertEqual(job.run(), 0)
                llm.assert_not_called()
                prod.assert_not_called()
            self.assertEqual([s['name'] for s in job.status['stages']], ['discover', 'triage'])


if __name__ == '__main__':
    unittest.main()
