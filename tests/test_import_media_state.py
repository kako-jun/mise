import sqlite3
import sys
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'favicon'))
import favicon
import import_media_state
from import_media_state import import_ledger


class FakeD1:
    """Tiny stand-in for Cloudflare D1/R2 that honours the importer's updated_at/lease guards."""

    def __init__(self, site_ids):
        self.sites = {s: {'progress_key': f'{s}.example/', 'favicon_path': None, 'thumb_path': None} for s in site_ids}
        self.jobs = {s: {'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None,
                         'updated_at': '2026-11-20 10:00:00', 'lease_owner': None} for s in site_ids}
        self.r2 = {}
        self.bump_after = None

    def get_r2(self, key):
        return self.r2.get(key)

    def put_r2(self, key, data, content_type='image/webp', before_attempt=None):
        if before_attempt:
            before_attempt()
        self.r2[key] = data

    def query(self, sql, params=()):
        if 'SET lease_owner=NULL' in sql:
            job = self.jobs[params[0]]
            if job['lease_owner'] == params[2]:
                job['lease_owner'] = None
            return []
        if 'SET lease_owner=?,lease_until=?' in sql:
            owner, _until, _key, sid, _kind = params
            self.jobs[sid]['lease_owner'] = owner
            if self.bump_after == 'lease':
                self.jobs[sid]['updated_at'] = '2026-11-24 09:00:00'
            return [{'site_id': sid}]
        if 'SELECT j.site_id' in sql:
            _key, sid, _kind, owner, h, a, upd, ptr = params
            job = self.jobs[sid]
            ok = (job['lease_owner'] == owner and job['content_hash'] == h and job['asset_key'] == a
                  and job['updated_at'] == upd and self.sites[sid]['favicon_path'] == ptr)
            return [{'site_id': sid}] if ok else []
        raise AssertionError(sql)

    def batch(self, statements):
        if statements[0][0].startswith('SELECT progress_key'):
            out = []
            for site_stmt, job_stmt in zip(statements[0::2], statements[1::2]):
                sid = job_stmt[1][0]
                cols = job_stmt[0].split(' FROM')[0].replace('SELECT ', '').split(',')
                out.append([dict(self.sites[sid])])
                out.append([{k: self.jobs[sid][k] for k in cols}])
            return out
        job_sql, jp = statements[0]
        site_sql, sp = statements[1]
        (state, _att, _cf, _err, _chk, _retry, _exc, h, a, meta, _key, sid, _kind, owner, g_h, g_a, g_upd) = jp
        job = self.jobs[sid]
        job_hit = job['lease_owner'] == owner and job['content_hash'] == g_h and job['asset_key'] == g_a and job['updated_at'] == g_upd
        if job_hit:
            job.update(state=state, content_hash=h, asset_key=a, metadata_json=meta, updated_at='2026-11-24 12:00:00')
        site_hit = False
        if job_hit:
            site = self.sites[sid]
            if site['favicon_path'] == sp[3]:
                site['favicon_path'] = sp[0]
                site_hit = True
        verify = {k: job[k] for k in ('state', 'content_hash', 'asset_key', 'metadata_json', 'lease_owner')}
        if job['lease_owner'] == owner:
            job['lease_owner'] = None
        return [[{'site_id': sid}] if job_hit else [], [{'id': sid}] if site_hit else [], [verify],
                [{'pointer': self.sites[sid]['favicon_path']}], []]


class ImportMediaStateTest(unittest.TestCase):
    def test_cli_defaults_to_read_only_dry_run(self):
        client = Mock()
        with patch.object(import_media_state, 'Cloudflare', return_value=client), \
             patch.object(import_media_state, 'import_ledger', return_value={'verified': [], 'ineligible': []}) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            result = import_media_state.main(['--kind', 'favicon', '--source', 'local.sqlite=assets'])
        self.assertEqual(result, 0)
        self.assertIs(run.call_args.kwargs['apply'], False)
        client.put_r2.assert_not_called()
        client.query.assert_not_called()

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
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': False, 'would_import': True, 'action': 'verified-existing-r2-object'}])
        self.assertFalse(result['ineligible'])
        client.put_r2.assert_not_called()
        client.query.assert_not_called()

    def test_dry_run_reports_local_only_asset_as_would_upload_without_writing(self):
        content = b'local-only-webp'
        content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger, assets = root / 'favicons.sqlite', root / 'assets'
            assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.get_r2.return_value = None
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=False)
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': False, 'would_import': True, 'action': 'would-upload-local-object'}])
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
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': True, 'action': 'uploaded-and-readback-verified'}])
        self.assertFalse(result['ineligible'])
        client.put_r2.assert_called_once()
        self.assertTrue(any('UPDATE site_media_jobs SET lease_owner=?,lease_until=?' in call.args[0] for call in client.query.call_args_list))
        self.assertIn('lease_owner=?', client.batch.call_args.args[0][0][0])

    def test_apply_checks_lease_before_r2_put_and_skips_if_lost(self):
        content = b'lease-expired-webp'
        content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.query.side_effect = [[{'site_id': 'site-1'}], []]
            client.get_r2.return_value = None
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertFalse(result['verified'])
        self.assertEqual(result['ineligible'][0]['reason'], 'D1 lease or media state changed before R2 upload')
        client.put_r2.assert_not_called()
        self.assertIn("julianday(j.lease_until)>julianday('now','+2 minutes')", client.query.call_args_list[1].args[0])
        self.assertTrue(any('lease_owner=NULL' in c.args[0] for c in client.query.call_args_list))

    def test_unexpected_r2_error_releases_owned_lease_and_row_is_ineligible(self):
        content = b'r2-error-webp'; content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            def query(sql, params=()):
                if 'UPDATE site_media_jobs SET lease_owner=?,lease_until=?' in sql:
                    return [{'site_id': 'site-1'}]
                raise RuntimeError('lease release network error')
            client.query.side_effect = query
            client.get_r2.side_effect = RuntimeError('original R2 read error')
            result = import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(result['verified'], [])
        self.assertEqual(result['ineligible'], [{'site_id': 'site-1', 'reason': 'R2/D1 transport error: original R2 read error'}])
        self.assertTrue(any('lease_owner=NULL' in c.args[0] for c in client.query.call_args_list))
        client.put_r2.assert_not_called()

    def test_transport_error_on_one_row_does_not_stop_other_rows(self):
        content = b'multi-row-webp'; content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            for sid in ('site-1', 'site-2'):
                (assets / favicon.site_name(sid)).write_bytes(content)
            db = favicon.connect(ledger)
            favicon.import_sites(db, [('site-1', 'https://a.example/'), ('site-2', 'https://b.example/')])
            db.execute("UPDATE favicons SET status='ok',icon_hash=?,changed_at='2026-11-24T00:00:00Z'", (content_hash,))
            db.commit(); db.close()
            fake = FakeD1(['site-1', 'site-2'])
            real_get = fake.get_r2
            def get_r2(key):
                if favicon.site_name('site-1') in key:
                    raise RuntimeError('R2 verify failed (400)')
                return real_get(key)
            fake.get_r2 = get_r2
            result = import_ledger(fake, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual([r['site_id'] for r in result['verified']], ['site-2'])
        self.assertEqual(result['ineligible'][0]['site_id'], 'site-1')
        self.assertTrue(result['ineligible'][0]['reason'].startswith('R2/D1 transport error: R2 verify failed (400)'))
        self.assertIsNone(fake.jobs['site-1']['lease_owner'])

    def test_apply_succeeds_when_production_row_has_non_null_updated_at(self):
        content = b'prod-row-webp'; content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            (assets / favicon.site_name('site-1')).write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            fake = FakeD1(['site-1'])
            self.assertIsNotNone(fake.jobs['site-1']['updated_at'])
            result = import_ledger(fake, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(result['ineligible'], [])
        self.assertEqual(result['verified'], [{'site_id': 'site-1', 'eligible_for_cleanup': True, 'action': 'uploaded-and-readback-verified'}])
        self.assertEqual(fake.jobs['site-1']['content_hash'], content_hash)
        self.assertEqual(fake.sites['site-1']['favicon_path'], favicon.site_name('site-1'))

    def test_row_whose_updated_at_changes_after_prefetch_is_still_refused(self):
        content = b'raced-row-webp'; content_hash = favicon.icon_name(content)
        for existing_r2 in (False, True):
            with self.subTest(existing_r2=existing_r2), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
                (assets / favicon.site_name('site-1')).write_bytes(content)
                self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
                fake = FakeD1(['site-1'])
                if existing_r2:
                    fake.r2[('icon/' + favicon.site_name('site-1'))] = content
                fake.bump_after = 'lease'  # concurrent writer lands after the lease, before the guarded write
                result = import_ledger(fake, 'favicon', [(ledger, assets)], apply=True)
                self.assertEqual(result['verified'], [])
                self.assertEqual(len(result['ineligible']), 1)
                self.assertIsNone(fake.jobs['site-1']['content_hash'])
                self.assertEqual(fake.jobs['site-1']['state'], 'pending')

    def test_local_asset_read_error_releases_owned_lease(self):
        content = b'read-error-webp'; content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            asset_path = assets / favicon.site_name('site-1'); asset_path.write_bytes(content)
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.query.side_effect = [[{'site_id': 'site-1'}], None]
            with patch.object(Path, 'read_bytes', side_effect=OSError('local disk read failed')):
                with self.assertRaisesRegex(OSError, 'local disk read failed'):
                    import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(client.query.call_count, 2)
        self.assertIn('lease_owner=NULL', client.query.call_args_list[1].args[0])

    def test_malformed_attempt_count_releases_lease_in_row_finally(self):
        content = b'malformed-attempts'; content_hash = favicon.icon_name(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root / 'favicons.sqlite'; assets = root / 'assets'; assets.mkdir()
            self.make_ledger(ledger, 'site-1', content_hash, '2026-11-24T00:00:00Z')
            db = favicon.connect(ledger)
            db.execute("UPDATE favicons SET consecutive_failures='not-an-integer' WHERE site_id='site-1'")
            db.commit(); db.close()
            client = Mock()
            client.batch.return_value = [
                [{'progress_key': 'example.org/', 'favicon_path': None, 'thumb_path': None}],
                [{'state': 'pending', 'content_hash': None, 'asset_key': None, 'metadata_json': None, 'updated_at': 'old'}],
            ]
            client.query.side_effect = [[{'site_id': 'site-1'}], None]
            client.get_r2.return_value = content
            with self.assertRaises(ValueError):
                import_ledger(client, 'favicon', [(ledger, assets)], apply=True)
        self.assertEqual(client.query.call_count, 2)
        self.assertIn('lease_owner=NULL', client.query.call_args_list[1].args[0])
        client.batch.assert_called_once()  # read-only D1 prefetch; no persistence batch

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
