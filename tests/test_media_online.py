import json
import ssl
import sys
import unittest
import io
import urllib.error
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import media_online
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'favicon'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'thumbnail'))
import favicon


class Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self, *_):
        return self.body


class MediaOnlineTest(unittest.TestCase):
    def test_d1_query_uses_authenticated_json_api_and_returns_rows(self):
        seen = []
        def opener(req, timeout):
            seen.append((req, timeout))
            return Response(json.dumps({'success': True, 'result': [{'results': [{'state': 'pending'}]}]}).encode())
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener)
        rows = client.query('SELECT state FROM site_media_jobs WHERE site_id=?', ('site-1',))
        self.assertEqual(rows, [{'state': 'pending'}])
        req, timeout = seen[0]
        self.assertEqual(req.method, 'POST')
        self.assertIn('/d1/database/database/query', req.full_url)
        self.assertEqual(json.loads(req.data), {'sql': 'SELECT state FROM site_media_jobs WHERE site_id=?', 'params': ['site-1']})
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret')

    def test_d1_batch_sends_bounded_prepared_statements(self):
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return Response(json.dumps({'success': True, 'result': [{'results': []}, {'results': [{'ok': 1}]}]}).encode())
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener)
        result = client.batch([('SELECT ? AS id', ('a',)), ('SELECT ? AS id', ('b',))])
        self.assertEqual(result, [[], [{'ok': 1}]])
        self.assertEqual(json.loads(seen[0].data), {'batch': [
            {'sql': 'SELECT ? AS id', 'params': ['a']}, {'sql': 'SELECT ? AS id', 'params': ['b']} ]})

    def test_cloudflare_retries_429_with_retry_after_and_bounded_attempts(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '0.25'}, io.BytesIO(b'rate limited'))
            return Response(json.dumps({'success': True, 'result': [{'results': [{'ok': 1}]}]}).encode())
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=4, retry_base_seconds=0.2, retry_max_seconds=2)
        with patch.object(media_online.time, 'sleep') as sleep, patch.object(media_online.random, 'random', return_value=0):
            self.assertEqual(client.query('SELECT 1'), [{'ok': 1}])
        self.assertEqual(len(calls), 3)
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [0.25, 0.25])

    def test_cloudflare_stops_after_bounded_429_retries(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'rate limited'))
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=3, retry_base_seconds=0, retry_max_seconds=0)
        with patch.object(media_online.time, 'sleep') as sleep, patch.object(media_online.random, 'random', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'Cloudflare API 429'):
                client.query('SELECT 1')
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_cloudflare_does_not_sleep_past_retry_after_safety_cap(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '1200'}, io.BytesIO(b'rate limited'))
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=7, max_retry_after_seconds=300)
        with patch.object(media_online.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'Cloudflare API 429'):
                client.query('SELECT 1')
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_r2_put_retry_window_cannot_outlive_configured_write_budget(self):
        calls = []; clock = [0.0]
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '0.75'}, io.BytesIO(b'rate limited'))
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=7, retry_base_seconds=0, retry_max_seconds=0,
                                         max_write_retry_seconds=1)
        def sleep(seconds): clock[0] += seconds
        with patch.object(media_online.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(media_online.time, 'sleep', side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, 'Cloudflare API 429'):
                client._request('https://example.test/object', method='PUT', body=b'bytes')
        self.assertEqual(len(calls), 2)
        self.assertEqual(clock[0], 0.75)

    def test_r2_put_revalidates_owner_and_object_before_each_retry(self):
        calls = []; validations = []
        def opener(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '0'}, io.BytesIO(b'rate limited'))
            return Response(b'{"success":true,"result":{}}')
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=3, retry_base_seconds=0, retry_max_seconds=0)
        with patch.object(media_online.time, 'sleep'), patch.object(media_online.random, 'random', return_value=0):
            client.put_r2('thumb/site.webp', b'bytes', before_attempt=lambda: validations.append('checked'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(validations, ['checked', 'checked'])

    def test_r2_put_aborts_retry_when_competing_object_changes(self):
        calls = []; validations = []
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '0'}, io.BytesIO(b'rate limited'))
        def check():
            validations.append(True)
            if len(validations) == 2:
                raise RuntimeError('R2 object changed before retry')
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener,
                                         max_attempts=3, retry_base_seconds=0, retry_max_seconds=0)
        with patch.object(media_online.time, 'sleep'), patch.object(media_online.random, 'random', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'R2 object changed'):
                client.put_r2('thumb/site.webp', b'bytes', before_attempt=check)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(validations), 2)

    def test_put_attempt_guard_can_query_d1_with_single_request_slot(self):
        methods = []
        def opener(req, timeout):
            methods.append(req.method)
            if req.method == 'POST':
                return Response(b'{"success":true,"result":[{"results":[{"site_id":"s1"}]}]}')
            return Response(b'{"success":true,"result":{}}')
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener, max_concurrent=1)
        client.put_r2('thumb/site.webp', b'bytes', before_attempt=lambda: client.query('SELECT 1'))
        self.assertEqual(methods, ['POST', 'PUT'])

    def test_d1_post_is_not_replayed_after_ambiguous_5xx(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 503, 'Service Unavailable', {}, io.BytesIO(b'transient'))
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener, max_attempts=5)
        with patch.object(media_online.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'Cloudflare API 503'):
                client.query('UPDATE site_media_jobs SET attempts=attempts+1')
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_importer_request_pacing_is_shared_and_optional(self):
        client = media_online.Cloudflare('account', 'secret', 'database', opener=lambda *_args, **_kwargs: Response(b'{"success":true,"result":[{"results":[]}] }'), min_request_interval=0.5)
        with patch.object(media_online.time, 'monotonic', return_value=10.0), patch.object(media_online.time, 'sleep') as sleep:
            client.query('SELECT 1')
            client.query('SELECT 1')
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(sleep.call_args.args[0], 0.5)

    def test_verified_opener_relaxes_only_x509_strict_extension_check(self):
        captured = {}
        built = Mock(open=Mock())
        def build(*handlers):
            captured['handlers'] = handlers
            return built
        with patch.object(media_online.urllib.request, 'build_opener', side_effect=build):
            opener = media_online.build_verified_opener()
        context = captured['handlers'][1]._context
        self.assertIs(opener, built.open)
        self.assertIsInstance(captured['handlers'][0], media_online.urllib.request.ProxyHandler)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        strict = getattr(ssl, 'VERIFY_X509_STRICT', 0)
        if strict:
            self.assertEqual(context.verify_flags & strict, 0)

    def test_r2_put_sends_image_bytes_without_a_file(self):
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return Response(b'{"success":true,"result":{}}')
        client = media_online.Cloudflare('account', 'secret', 'database', opener=opener)
        client.put_r2('thumb/abc.webp', b'webp bytes')
        self.assertEqual(seen[0].method, 'PUT')
        self.assertEqual(seen[0].data, b'webp bytes')
        self.assertEqual(seen[0].get_header('Content-type'), 'image/webp')
        self.assertEqual(seen[0].full_url,
                         'https://api.cloudflare.com/client/v4/accounts/account/r2/buckets/mise-media/objects/thumb/abc.webp')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer secret')

    def test_r2_get_uses_cloudflare_rest_object_endpoint_and_bearer_auth(self):
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return Response(b'webp bytes')
        client = media_online.Cloudflare('account', 'secret', 'database', 'mise-media', opener=opener)
        self.assertEqual(client.get_r2('icon/site-id.webp'), b'webp bytes')
        self.assertEqual(seen[0].get_method(), 'GET')
        self.assertEqual(seen[0].full_url,
                         'https://api.cloudflare.com/client/v4/accounts/account/r2/buckets/mise-media/objects/icon/site-id.webp')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer secret')

    def test_cloudflare_account_id_can_be_discovered_with_account_list_permission(self):
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return Response(json.dumps({'success': True, 'result': [{'id': 'discovered-account'}]}).encode())
        with patch.dict(media_online.os.environ, {}, clear=True):
            client = media_online.Cloudflare(token='secret', database='database', bucket='mise-media', opener=opener)
        self.assertEqual(client.account, 'discovered-account')
        self.assertEqual(seen[0].full_url, 'https://api.cloudflare.com/client/v4/accounts?per_page=1')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer secret')

    def test_memory_store_uploads_to_the_configured_prefix(self):
        client = Mock()
        store = media_online.R2MemoryStore(client, 'icon/')
        self.assertTrue(store.put('id.webp', b'bytes'))
        client.put_r2.assert_called_once_with('icon/id.webp', b'bytes')

    def test_run_kind_uses_atomic_d1_lease_claim_and_releases_after_processing(self):
        now = media_online.datetime.datetime(2026, 11, 24, tzinfo=media_online.datetime.timezone.utc)
        client = Mock()
        client.query.side_effect = [
            [{'identity_key': 'example.org/', 'site_id': 'site-1'}],
            [{'identity_key': 'example.org/', 'site_id': 'site-1', 'metadata_json': None, 'url': 'https://example.org/'}],
        ]
        def batch(statements):
            p = statements[1][1]
            key = statements[0][1][0]
            return [[{'id': 'site-1'}], [{'site_id': 'site-1'}],
                    [{'state': p[0], 'content_hash': p[5], 'asset_key': p[6], 'metadata_json': p[7],
                      'lease_owner': p[12]}], [{'pointer': key}], [{'site_id': 'site-1'}]]
        client.batch.side_effect = batch
        def run(db, store, **kwargs):
            db.execute("UPDATE favicons SET status='ok',icon_hash='hash',last_checked_at=?,next_check_at=?,changed_at=? WHERE site_id='site-1'",
                       ('2026-11-24T00:00:00Z', '2026-12-24T00:00:00Z', '2026-11-24T00:00:00Z'))
        with patch.object(favicon, 'run', side_effect=run):
            self.assertEqual(media_online.run_kind(client, 'favicon', 1, now=now, log=lambda *_: None), 1)
        claim_sql = client.query.call_args_list[0].args[0]
        statements = client.batch.call_args.args[0]
        persist_sql = statements[1][0]
        self.assertIn('UPDATE site_media_jobs SET lease_owner=?,lease_until=?', claim_sql)
        self.assertIn('RETURNING identity_key,site_id', claim_sql)
        self.assertIn("j.kind!='thumbnail' OR j.eligible=1", claim_sql)
        self.assertIn('lease_owner=?', persist_sql)
        self.assertIn('UPDATE sites SET favicon_path', statements[0][0])

    def test_favicon_claim_does_not_apply_thumbnail_adult_eligibility(self):
        # The shared claim SQL keeps favicon jobs eligible even when adult=yes;
        # only thumbnail jobs are gated by the durable D1 eligibility column.
        now = media_online.datetime.datetime(2026, 11, 24, tzinfo=media_online.datetime.timezone.utc)
        client = Mock()
        client.query.side_effect = [[], []]
        self.assertEqual(media_online.run_kind(client, 'favicon', 1, now=now, log=lambda *_: None), 0)
        sql = client.query.call_args_list[0].args[0]
        self.assertIn("j.kind!='thumbnail' OR j.eligible=1", sql)

    def test_thumbnail_daily_quota_reservation_never_exceeds_cap(self):
        now = media_online.datetime.datetime(2026, 11, 24, tzinfo=media_online.datetime.timezone.utc)
        client = Mock()
        client.query.side_effect = [
            [{'identity_key': f'{i}.example/', 'site_id': f's{i}'} for i in range(2)],
            [{'identity_key': f'{i}.example/', 'site_id': f's{i}', 'metadata_json': None, 'url': f'https://{i}.example/'} for i in range(2)],
            [],
            [],
            [],
        ]
        with patch('thumbnail.run') as run:
            self.assertEqual(media_online.run_kind(client, 'thumbnail', 2, now=now, log=lambda *_: None), 0)
        reserve_sql = client.query.call_args_list[3].args[0]
        self.assertIn('attempts<3000 RETURNING attempts,reserved', reserve_sql)
        run.assert_not_called()

    def test_long_batch_renews_the_owned_lease(self):
        now = media_online.datetime.datetime.now(media_online.datetime.timezone.utc)
        client = Mock()
        client.query.side_effect = [
            [{'identity_key': 'example.org/', 'site_id': 'site-1'}],
            [{'identity_key': 'example.org/', 'site_id': 'site-1', 'metadata_json': None, 'url': 'https://example.org/'}],
            [{'site_id': 'site-1'}],
        ]
        def batch(statements):
            p = statements[1][1]
            return [[{'id': 'site-1'}], [{'site_id': 'site-1'}],
                    [{'state': p[0], 'content_hash': p[5], 'asset_key': p[6], 'metadata_json': p[7],
                      'lease_owner': p[12]}], [{'pointer': statements[0][1][0]}], [{'site_id': 'site-1'}]]
        client.batch.side_effect = batch
        def run(db, store, **kwargs):
            db.execute("UPDATE favicons SET status='ok',icon_hash='hash',last_checked_at=?,next_check_at=?,changed_at=? WHERE site_id='site-1'",
                       (now.isoformat(), now.isoformat(), now.isoformat()))
            kwargs['log']('completed capture')
        with patch.object(favicon, 'run', side_effect=run), patch.object(media_online.time, 'monotonic', side_effect=[0, 301, 302]):
            self.assertEqual(media_online.run_kind(client, 'favicon', 1, now=now, log=lambda *_: None), 1)
        heartbeat_sql = client.query.call_args_list[2].args[0]
        self.assertIn('SET lease_until=?', heartbeat_sql)
        self.assertIn('RETURNING site_id', heartbeat_sql)


if __name__ == '__main__':
    unittest.main()
