import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import media_worker
import nightly_media


class MediaWorkerTest(unittest.TestCase):
    def test_nightly_media_processes_remote_d1_jobs_without_local_paths(self):
        client = Mock()
        with patch.object(nightly_media, 'Cloudflare', return_value=client), \
             patch.object(nightly_media, 'run_kind', side_effect=[2, 1]) as run:
            self.assertEqual(nightly_media.main(['--favicon-limit', '2', '--thumbnail-limit', '1']), 0)
        self.assertEqual([call.args[1:] for call in run.call_args_list], [('favicon', 2), ('thumbnail', 1)])
        self.assertTrue(all(call.args[0] is client for call in run.call_args_list))

    def test_worker_invokes_online_queue_without_var_or_queue_file(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(media_worker, 'sys', SimpleNamespace(platform='win32')), \
             patch.object(media_worker.media, 'main', return_value=0) as run:
            self.assertEqual(media_worker.main(['--once']), 0)
            self.assertEqual(run.call_args.args[0], ['--favicon-limit', '100', '--thumbnail-limit', '10'])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_worker_rejects_invalid_limits(self):
        with patch.object(media_worker, 'sys', SimpleNamespace(platform='win32')):
            with self.assertRaises(SystemExit):
                media_worker.main(['--once', '--favicon-limit', '101'])


if __name__ == '__main__':
    unittest.main()
