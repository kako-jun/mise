import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'favicon'))
import favicon
from import_media_state import import_ledger


class ImportMediaStateTest(unittest.TestCase):
    def make_ledger(self, path, site_id, icon_hash, changed_at):
        db = favicon.connect(path)
        favicon.import_sites(db, [(site_id, 'https://example.org/')])
        db.execute("UPDATE favicons SET status='ok',icon_hash=?,changed_at=?,last_checked_at=? WHERE site_id=?",
                   (icon_hash, changed_at, changed_at, site_id))
        db.commit(); db.close()

    def test_dry_run_verifies_existing_remote_bytes_without_writing_d1_or_r2(self):
        content = b'test-webp-content'
        content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'favicons.sqlite'
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.get_r2.return_value = content
            result = import_ledger(client, 'favicon', [(ledger, Path(tmp))], apply=False)
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': False, 'would_import': True}])
        self.assertFalse(result['ineligible'])
        client.put_r2.assert_not_called()
        client.query.assert_not_called()

    def test_same_timestamp_conflicting_ledgers_are_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.sqlite', Path(tmp) / 'b.sqlite'
            self.make_ledger(a, 'site-1', 'hash-a', '2026-11-24T00:00:00Z')
            self.make_ledger(b, 'site-1', 'hash-b', '2026-11-24T00:00:00Z')
            client = Mock()
            result = import_ledger(client, 'favicon', [(a, Path(tmp)), (b, Path(tmp))], apply=True)
        self.assertEqual(result['ineligible'], [{'site_id': 'site-1', 'reason': 'same-time local ledgers disagree'}])
        client.put_r2.assert_not_called()
        client.batch.assert_not_called()

    def test_newest_legacy_row_wins_before_r2_verification(self):
        older, newer = b'older-webp', b'newer-webp'
        old_hash, new_hash = favicon.icon_name(older), favicon.icon_name(newer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / 'a.sqlite', root / 'b.sqlite'
            old_assets, new_assets = root / 'old', root / 'new'
            old_assets.mkdir(); new_assets.mkdir()
            name = favicon.site_name('site-1')
            (old_assets / name).write_bytes(older); (new_assets / name).write_bytes(newer)
            self.make_ledger(a, 'site-1', old_hash, '2026-11-23T00:00:00Z')
            self.make_ledger(b, 'site-1', new_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.get_r2.return_value = newer
            result = import_ledger(client, 'favicon', [(a, old_assets), (b, new_assets)], apply=False)
        self.assertEqual(result['verified'][0]['site_id'], 'site-1')
        self.assertFalse(result['ineligible'])
        client.put_r2.assert_not_called()

    def test_apply_marks_cleanup_eligible_only_after_d1_readback(self):
        content = b'verified-webp'
        content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, assets = root / 'favicons.sqlite', root / 'assets'
            assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.side_effect = [
                [[{'progress_key': 'example.org/'}], [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None}]],
                [[], [], [{'state': 'ok', 'content_hash': content_hash,
                           'asset_key': favicon.site_name('site-1'), 'metadata_json': None}]],
            ]
            # Fill the expected metadata readback from the update parameters.
            def batch(statements):
                if len(statements) == 2:
                    return [[{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                            [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}]]
                job_params = statements[0][1]
                site_params = statements[1][1]
                return [[{'site_id': 'site-1'}], [{'id': 'site-1'}],
                        [{'state': job_params[0], 'content_hash': job_params[7], 'asset_key': job_params[8],
                          'metadata_json': job_params[9], 'lease_owner': job_params[13]}],
                        [{'pointer': site_params[0]}], [{'site_id': 'site-1'}]]
            client.batch.side_effect = batch
            client.query.return_value = [{'site_id': 'site-1'}]
            client.get_r2.side_effect = [None, content]
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': True}])
        self.assertFalse(result['ineligible'])
        client.put_r2.assert_called_once()
        self.assertTrue(any('UPDATE site_media_jobs SET lease_owner=?,lease_until=?' in call.args[0] for call in client.query.call_args_list))
        self.assertIn('lease_owner=?', client.batch.call_args.args[0][0][0])

    def test_excluded_local_image_never_changes_site_pointer_or_becomes_cleanup_eligible(self):
        content = b'excluded-webp'
        content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, assets = root / 'favicons.sqlite', root / 'assets'
            assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            db = favicon.connect(ledger)
            db.execute("UPDATE favicons SET exclude=1 WHERE site_id='site-1'"); db.commit(); db.close()
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(result['ineligible'][0]['site_id'], 'site-1')
        self.assertFalse(result['verified'])
        client.get_r2.assert_not_called()
        client.put_r2.assert_not_called()
        client.query.assert_not_called()

    def test_contentless_excluded_row_is_ineligible_when_fixed_r2_asset_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, assets = root / 'favicons.sqlite', root / 'assets'
            assets.mkdir()
            self.make_ledger(ledger, 'site-1', None, '2026-11-24T00:00:00Z')
            db = favicon.connect(ledger)
            db.execute("UPDATE favicons SET status='none',exclude=1 WHERE site_id='site-1'"); db.commit(); db.close()
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.query.return_value = [{'site_id': 'site-1'}]
            client.get_r2.return_value = b'preexisting-object'
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertIn('R2 asset exists', result['ineligible'][0]['reason'])
        self.assertFalse(result['verified'])
        client.put_r2.assert_not_called()
        self.assertTrue(any('UPDATE site_media_jobs SET lease_owner=?,lease_until=?' in call.args[0] for call in client.query.call_args_list))


if __name__ == '__main__':
    unittest.main()
