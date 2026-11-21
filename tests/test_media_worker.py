import json
import contextlib
import datetime
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import media_worker as worker
import nightly_media as media


class MediaWorkerTest(unittest.TestCase):
    def test_linux_thumbnail_retains_default_capture_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(var=Path(tmp))
            now = datetime.datetime.now(datetime.timezone.utc)
            capturer = MagicMock(playwright='/playwright', chromium='/chromium')
            with patch.object(media, 'sys', SimpleNamespace(platform='linux')), \
                 patch.object(media.shutil, 'which', return_value='/usr/bin/bwrap'), \
                 patch.object(media.thumbnail, 'NodeCapturer', return_value=capturer) as ctor, \
                 patch.object(media.thumbnail, 'due_sites', return_value=[{'site_id': 's1'}]), \
                 patch.object(media.thumbnail, 'run'), \
                 patch.object(media.thumbnail, 'upload'), \
                 patch.object(media.thumbnail, 'sql_updates', return_value=''), \
                 patch.object(media, 'retry_dates', return_value={}):
                media.capture_thumbnails(args, [{'id': 's1', 'url': 'https://example.com/'}], ['s1'], now)
                ctor.assert_called_once_with()
                capturer.check_ready.assert_called_once()
                capturer.check_upstream.assert_called_once()

    def test_windows_favicon_only_keeps_thumbnails_and_never_calls_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nightly = root / 'nightly'
            cycle = nightly / '2026-11-21-2'
            cycle.mkdir(parents=True)
            (nightly / 'media-worker-stopped.txt').write_text('old thumbnail stop', encoding='utf-8')
            (cycle / 'status.json').write_text(json.dumps({'ok': True, 'finished_at': 'yes',
                'prod_applied': True, 'stages': [{'name': 'prod', 'status': 'ok'}]}), encoding='utf-8')
            def enqueue(db, keys, snapshot):
                db.executemany('INSERT INTO pending VALUES(?,?,?)', [('favicon', 's1', ''), ('thumbnail', 's1', '')])
            def process(args):
                self.assertNotIn('--wsl-distro', args)
                self.assertEqual(args[args.index('--thumbnail-limit') + 1], '0')
                return 0
            with patch.object(worker, 'sys', SimpleNamespace(platform='win32')), \
                    patch.object(media, 'enqueue', side_effect=enqueue), \
                    patch.object(media, 'main', side_effect=process), \
                    patch.object(media, 'capture_in_wsl', side_effect=AssertionError('WSL must not start')):
                self.assertEqual(worker.main(['--var', str(root), '--once', '--thumbnail-limit', '0']), 0)
            with contextlib.closing(sqlite3.connect(nightly / 'media.sqlite')) as db:
                self.assertEqual(db.execute("SELECT site_id FROM pending WHERE kind='thumbnail'").fetchall(), [('s1',)])

    def test_windows_thumbnail_mode_needs_no_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(worker, 'sys', SimpleNamespace(platform='win32')), \
                 patch.object(media, 'main', return_value=0) as run:
                self.assertEqual(worker.main(['--var', tmp, '--once', '--thumbnail-limit', '1']), 0)
                self.assertNotIn('--wsl-distro', run.call_args.args[0])

    def test_wsl_options_are_rejected(self):
        for args in (['--wsl-distro', 'Arch'], ['--wsl-playwright', '/x'], ['--wsl-chromium', '/x']):
            with self.assertRaises(SystemExit):
                worker.main(['--var', '/unused', '--once', *args])
            with self.assertRaises(SystemExit):
                media.main(['--var', '/unused', '--out', '/unused', '--d1-name', 'mise',
                            '--favicon-limit', '0', '--thumbnail-limit', '0', *args])

    def test_only_successful_production_cycles_are_enqueued_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, prod in [('ok', True), ('pending', False), ('failed', False)]:
                p = root / name
                p.mkdir()
                (p / 'status.json').write_text(json.dumps({'ok': name != 'failed', 'finished_at': 'yes',
                    'prod_applied': prod, 'stages': [{'name': 'prod', 'status': 'ok'}]}), encoding='utf-8')
            db = sqlite3.connect(':memory:')
            with patch.object(media, 'enqueue') as enqueue:
                worker.collect(root, db)
                worker.collect(root, db)
                self.assertEqual(enqueue.call_count, 1)
                self.assertEqual(enqueue.call_args.args[1], root / 'ok' / 'keys.txt')
            db.close()

    def test_missing_export_does_not_block_other_cycles_and_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('bad', 'good'):
                p = root / name
                p.mkdir()
                (p / 'status.json').write_text(json.dumps({'ok': True, 'finished_at': 'yes',
                    'prod_applied': True, 'stages': [{'name': 'prod', 'status': 'ok'}]}), encoding='utf-8')
            db = sqlite3.connect(':memory:')
            def enqueue(db, keys, snapshot):
                if keys.parent.name == 'bad':
                    raise FileNotFoundError('missing')
            with patch.object(media, 'enqueue', side_effect=enqueue):
                worker.collect(root, db)
            self.assertEqual(db.execute('SELECT name FROM media_cycles').fetchall(), [('good',)])
            db.close()

    def test_wsl_timeout_leaves_persistent_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'nightly').mkdir()
            a = SimpleNamespace(var=root, out=root, wsl_distro='Arch', wsl_playwright='/app/playwright', wsl_chromium='/app/chromium')
            with patch.object(media, 'wsl_path', return_value='/tmp/x'), \
                    patch.object(media.subprocess, 'run', side_effect=subprocess.TimeoutExpired('wsl', 1)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    media.capture_in_wsl(a, root / 'db', root / 'store', [], [])
            self.assertTrue((root / 'nightly' / 'media-worker-stopped.txt').exists())

    def test_wsl_success_removes_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'nightly').mkdir()
            a = SimpleNamespace(var=root, out=root, wsl_distro='Arch', wsl_playwright='/app/playwright', wsl_chromium='/app/chromium')
            with patch.object(media, 'wsl_path', return_value='/tmp/x'), patch.object(media.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
                media.capture_in_wsl(a, root / 'db', root / 'store', [], [])
            self.assertFalse((root / 'nightly' / 'media-worker-stopped.txt').exists())

    def test_wsl_failure_preserves_marker_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'nightly').mkdir()
            a = SimpleNamespace(var=root, out=root, wsl_distro='Arch', wsl_playwright='/app/playwright', wsl_chromium='/app/chromium')
            with patch.object(media, 'wsl_path', return_value='/tmp/x'), patch.object(media.subprocess, 'run', return_value=SimpleNamespace(returncode=3)):
                with self.assertRaises(subprocess.CalledProcessError):
                    media.capture_in_wsl(a, root / 'db', root / 'store', [], [])
            self.assertTrue((root / 'nightly' / 'media-worker-stopped.txt').exists())


if __name__ == '__main__':
    unittest.main()
