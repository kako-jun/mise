"""Direct, memory-only clients for the online D1 media queue and R2 assets."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import datetime
import hashlib
import re
import sqlite3
import ssl
import sys
import time
import uuid
from pathlib import Path

TOKEN = os.environ.get('CLOUDFLARE_API_TOKEN')
CONFIG = Path(__file__).resolve().parents[1] / 'wrangler.jsonc'
CONFIG_TEXT = CONFIG.read_text(encoding='utf-8') if CONFIG.is_file() else ''
CONFIG_ACCOUNT = next(iter(re.findall(r'"account_id"\s*:\s*"([a-f0-9]+)"', CONFIG_TEXT)), None)
DATABASE = os.environ.get('MISE_D1_DATABASE_ID') or next(iter(re.findall(r'"database_id"\s*:\s*"([a-f0-9-]+)"', CONFIG_TEXT)), None)
BUCKET = os.environ.get('MISE_R2_BUCKET') or next(iter(re.findall(r'"bucket_name"\s*:\s*"([^"]+)"', CONFIG_TEXT)), 'mise-media')


def build_verified_opener():
    """Use system/proxy CA roots and retain hostname/chain checks.

    Some intercepted proxy CA bundles omit an extension required by OpenSSL strict
    X509 mode. Relax only that extension check; CERT_REQUIRED and check_hostname stay on.
    """
    context = ssl.create_default_context()
    strict = getattr(ssl, 'VERIFY_X509_STRICT', 0)
    context.verify_flags &= ~strict
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=context),
    ).open


class Cloudflare:
    def __init__(self, account=None, token=TOKEN, database=DATABASE, bucket=BUCKET, opener=None):
        if not token:
            raise RuntimeError('CLOUDFLARE_API_TOKEN が必要です')
        self.account, self.token, self.database, self.bucket = account or os.environ.get('CLOUDFLARE_ACCOUNT_ID') or CONFIG_ACCOUNT, token, database, bucket
        self.opener = opener or build_verified_opener()
        if not self.account:
            accounts = self._request('https://api.cloudflare.com/client/v4/accounts?per_page=1')
            found = accounts.get('result') or []
            if not found or not found[0].get('id'):
                raise RuntimeError('Cloudflare API からアカウント ID を取得できません')
            self.account = found[0]['id']

    def _request(self, url, method='GET', body=None, content_type=None):
        headers = {'Authorization': f'Bearer {self.token}'}
        if content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(req, timeout=60) as response:
                data = response.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'Cloudflare API {e.code}: {e.read(500).decode("utf-8", "replace")}') from e
        result = json.loads(data) if data else {}
        if not result.get('success', True):
            raise RuntimeError(f'Cloudflare API error: {result.get("errors", [])}')
        return result

    def query(self, sql, params=()):
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account}/d1/database/{self.database}/query'
        body = json.dumps({'sql': sql, 'params': list(params)}).encode()
        result = self._request(url, 'POST', body, 'application/json')
        # D1 query returns one result record for the single statement.
        item = result.get('result', [{}])[0]
        return item.get('results', [])

    def batch(self, statements):
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account}/d1/database/{self.database}/query'
        body = json.dumps({'batch': [{'sql': sql, 'params': list(params)} for sql, params in statements]}).encode()
        result = self._request(url, 'POST', body, 'application/json')
        return [item.get('results', []) for item in result.get('result', [])]

    def put_r2(self, key, data, content_type='image/webp'):
        encoded = urllib.parse.quote(key, safe='/')
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account}/r2/buckets/{self.bucket}/objects/{encoded}'
        self._request(url, 'PUT', data, content_type)

    def get_r2(self, key):
        encoded = urllib.parse.quote(key, safe='/')
        url = f'https://api.cloudflare.com/client/v4/accounts/{self.account}/r2/buckets/{self.bucket}/objects/{encoded}'
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {self.token}'})
        try:
            with self.opener(req, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError(f'R2 verify failed ({e.code})') from e


class R2MemoryStore:
    """Adapter for favicon/thumbnail acquisition APIs; no temporary file is created."""
    label = 'r2-remote'
    trust_ledger = True

    def __init__(self, client, prefix):
        self.client, self.prefix = client, prefix

    def exists(self, name):
        return self.client.get_r2(self.prefix + name) is not None

    def put(self, name, data):
        try:
            self.client.put_r2(self.prefix + name, data)
        except Exception as error:
            raise OSError('R2 upload failed') from error
        return True

    def delete(self, name):
        raise NotImplementedError('online media path does not delete assets')


def run_kind(client, kind, limit, now=None, log=print):
    """Claim due D1 jobs, process in an in-memory ledger, publish bytes to R2, then persist state.

    The media modules retain their mature fetch/capture and retry logic, while their DB is
    strictly in-memory and their store writes bytes directly to Cloudflare's R2 API.
    """
    if kind not in ('favicon', 'thumbnail'):
        raise ValueError('unknown media kind')
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_s = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    owner = str(uuid.uuid4())
    lease_duration = datetime.timedelta(minutes=20)
    lease_until = (now + lease_duration).isoformat(timespec='seconds').replace('+00:00', 'Z')
    column = 'favicon_path' if kind == 'favicon' else 'thumb_path'
    claimed = client.query(
        f"UPDATE site_media_jobs SET lease_owner=?,lease_until=? WHERE rowid IN ("
        f"SELECT j.rowid FROM site_media_jobs j JOIN sites s ON s.id=j.site_id "
        f"WHERE j.kind=? AND j.excluded=0 AND j.state IN ('pending','failed','none') "
        f"AND (j.kind!='thumbnail' OR j.eligible=1) "
        f"AND (j.next_retry_at IS NULL OR j.next_retry_at<=?) AND s.{column} IS NULL "
        "AND (j.lease_until IS NULL OR j.lease_until<=?) ORDER BY COALESCE(j.next_retry_at,''),j.site_id LIMIT ?) "
        "RETURNING identity_key,site_id", (owner, lease_until, kind, now_s, now_s, max(0, limit)))
    due = client.query('SELECT j.identity_key,j.site_id,j.metadata_json,j.content_hash,j.asset_key,j.updated_at,s.url FROM site_media_jobs j JOIN sites s ON s.id=j.site_id WHERE j.kind=? AND j.lease_owner=? ORDER BY j.next_retry_at,j.site_id',
                       (kind, owner)) if claimed else []
    if not due:
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'favicon'))
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'thumbnail'))
    import favicon
    import thumbnail
    if kind == 'favicon':
        db = favicon.connect(':memory:')
        store = R2MemoryStore(client, 'icon/')
        favicon.import_sites(db, [(r['site_id'], r['url']) for r in due])
        records = 'favicons'
    else:
        db = thumbnail.connect(':memory:')
        store = R2MemoryStore(client, 'thumb/')
        thumbnail.import_sites(db, [(r['site_id'], r['url']) for r in due])
        today = now_s[:10]
        client.query('INSERT OR IGNORE INTO site_media_daily_counts(kind,day,attempts) VALUES(?,?,0)', (kind, today))
        reservation = client.query(
            "UPDATE site_media_daily_counts SET reserved=MIN(?,3000-attempts),attempts=attempts+MIN(?,3000-attempts) "
            "WHERE kind=? AND day=? AND attempts<3000 RETURNING attempts,reserved",
            (len(due), len(due), kind, today))
        if not reservation:
            client.query('UPDATE site_media_jobs SET lease_owner=NULL,lease_until=NULL WHERE kind=? AND lease_owner=?', (kind, owner))
            db.close()
            return 0
        reserved = int(reservation[0]['reserved'])
        reserved_total = int(reservation[0]['attempts'])
        if reserved < len(due):
            extra_ids = [r['identity_key'] for r in due[reserved:]]
            for identity in extra_ids:
                client.query('UPDATE site_media_jobs SET lease_owner=NULL,lease_until=NULL WHERE identity_key=? AND kind=? AND lease_owner=?',
                             (identity, kind, owner))
            due = due[:reserved]
        db.execute('INSERT OR REPLACE INTO day_counts(day,n) VALUES(?,?)', (today, reserved_total - len(due)))
        records = 'thumbs'
    # Restore each job's last response validators/retry state from D1 metadata.
    for job in due:
        metadata = json.loads(job['metadata_json'] or '{}')
        allowed = {row[1] for row in db.execute(f'PRAGMA table_info({records})')}
        metadata = {k: v for k, v in metadata.items() if k in allowed and k != 'site_id'}
        if metadata:
            cols = ','.join(f'"{k}"=?' for k in metadata)
            db.execute(f'UPDATE {records} SET {cols} WHERE site_id=?', (*metadata.values(), job['site_id']))
        content = metadata.get('icon_hash' if kind == 'favicon' else 'thumb_hash')
        if content and kind == 'favicon':
            db.execute('INSERT OR REPLACE INTO icon_put(site_id,store,content) VALUES(?,?,?)', (job['site_id'], store.label, content))
        elif content:
            db.execute('INSERT OR REPLACE INTO thumb_stored(name,store,content) VALUES(?,?,?)',
                       (thumbnail.thumb_name(job['site_id']), store.label, content))
    last_heartbeat = time.monotonic()
    def report(*args):
        nonlocal last_heartbeat
        if time.monotonic() - last_heartbeat >= 300:
            beat = datetime.datetime.now(datetime.timezone.utc)
            beat_s = beat.isoformat(timespec='seconds').replace('+00:00', 'Z')
            until = (beat + lease_duration).isoformat(timespec='seconds').replace('+00:00', 'Z')
            renewed = client.query('UPDATE site_media_jobs SET lease_until=? WHERE kind=? AND lease_owner=? AND lease_until>? RETURNING site_id',
                                   (until, kind, owner, beat_s))
            if len(renewed) != len(due):
                raise RuntimeError('D1 media lease lost during processing')
            last_heartbeat = time.monotonic()
        log(*args)
    if kind == 'favicon':
        favicon.run(db, store, now=now, limit=limit, site_ids=[r['site_id'] for r in due], log=report)
    else:
        capturer = thumbnail.NodeCapturer(sandbox=False, limits=False) if sys.platform == 'win32' else thumbnail.NodeCapturer()
        capturer.check_ready(); capturer.check_upstream()
        if not capturer.playwright or not capturer.chromium:
            raise RuntimeError('thumbnail capture requires Playwright and Chromium')
        thumbnail.run(db, store, capturer, now=now, limit=limit, site_ids=[r['site_id'] for r in due], log=report)
    id_column = 'site_id'
    for job in due:
        row = db.execute(f'SELECT * FROM {records} WHERE {id_column}=?', (job['site_id'],)).fetchone()
        data = dict(row)
        if kind == 'favicon':
            state = 'ok' if data.get('status') == 'ok' else ('none' if data.get('status') == 'none' else 'failed')
            attempts = int(data.get('consecutive_failures') or 0)
            checked = data.get('last_checked_at')
            retry = data.get('next_check_at')
            err = data.get('status') if state == 'failed' else None
            asset = favicon.site_name(job['site_id']) if data.get('icon_hash') else None
            content = data.get('icon_hash')
        else:
            state = data.get('state') if data.get('state') in ('ok','pending','failed') else 'failed'
            attempts = int(data.get('consecutive_failures') or 0)
            checked = data.get('last_checked_at')
            retry = data.get('next_check_at')
            err = data.get('last_error')
            asset = thumbnail.thumb_name(job['site_id']) if data.get('thumb_hash') else None
            content = data.get('thumb_hash')
        data.pop('site_id', None)
        metadata_json = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        now_check = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        lease_guard = "site_id=? AND kind=? AND lease_owner=? AND julianday(lease_until)>julianday('now')"
        if state == 'ok' and content:
            if kind == 'favicon':
                publish_sql = ("UPDATE sites SET favicon_path=?,favicon_v=? WHERE id=? AND favicon_path IS NULL AND EXISTS ("
                               "SELECT 1 FROM site_media_jobs WHERE " + lease_guard + " AND content_hash IS ? AND asset_key IS ? AND updated_at IS ?)")
                publish_params = (asset, favicon.icon_v(content), job['site_id'], job['site_id'], kind, owner,
                                  job.get('content_hash'), job.get('asset_key'), job.get('updated_at'))
            else:
                publish_sql = ("UPDATE sites SET thumb_path=?,thumb_lqip=?,thumb_captured_at=? WHERE id=? AND thumb_path IS NULL AND EXISTS ("
                               "SELECT 1 FROM site_media_jobs WHERE " + lease_guard + " AND content_hash IS ? AND asset_key IS ? AND updated_at IS ?)")
                publish_params = (asset, data.get('lqip'), data.get('captured_at'), job['site_id'], job['site_id'], kind, owner,
                                  job.get('content_hash'), job.get('asset_key'), job.get('updated_at'))
        else:
            publish_sql, publish_params = ('SELECT id FROM sites WHERE id=?', (job['site_id'],))
        update_guard = (lease_guard + " AND state='ok' AND content_hash IS ? AND asset_key IS ?" if state == 'ok' and content else
                        lease_guard + " AND content_hash IS ? AND asset_key IS ? AND updated_at IS ?")
        update_sql = ("UPDATE site_media_jobs SET state=?,attempts=attempts+1,consecutive_failures=?,last_error=?,last_checked_at=?,next_retry_at=?,content_hash=?,asset_key=?,metadata_json=?,updated_at=? "
                      "WHERE identity_key=? AND " + update_guard + " RETURNING site_id")
        guard_values = ((job.get('content_hash'), asset) if state == 'ok' and content else
                        (job.get('content_hash'), job.get('asset_key'), job.get('updated_at')))
        update_params = (state, attempts, err, checked, retry, content, asset, metadata_json, now_s,
                         job['identity_key'], job['site_id'], kind, owner, *guard_values)
        # The site trigger changes the job's updated_at; a successful image update is guarded by the
        # new asset key and live lease. Non-image results retain the original D1 snapshot guard.
        results = client.batch([
            (publish_sql, publish_params),
            (update_sql, update_params),
            ('SELECT state,content_hash,asset_key,metadata_json,lease_owner FROM site_media_jobs WHERE identity_key=? AND kind=? AND site_id=?',
             (job['identity_key'], kind, job['site_id'])),
            (f"SELECT {'favicon_path' if kind == 'favicon' else 'thumb_path'} AS pointer FROM sites WHERE id=?", (job['site_id'],)),
            ('UPDATE site_media_jobs SET lease_owner=NULL,lease_until=NULL WHERE identity_key=? AND kind=? AND site_id=? AND lease_owner=? RETURNING site_id',
             (job['identity_key'], kind, job['site_id'], owner)),
        ])
        published = (results[0] or [None])[0]
        saved = (results[1] or [None])[0]
        verify = (results[2] or [None])[0]
        site_row = (results[3] or [None])[0]
        expected_pointer = asset if state == 'ok' and content else None
        if (state == 'ok' and content and not published) or not saved or not verify or not site_row or site_row.get('pointer') != expected_pointer:
            log(f"{job['site_id']}\tlease_lost\tresult not persisted")
            continue
        if verify.get('state') != state or verify.get('content_hash') != content or verify.get('asset_key') != asset or verify.get('metadata_json') != metadata_json:
            log(f"{job['site_id']}\td1_readback_mismatch")
    db.close()
    return len(due)
