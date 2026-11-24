"""One-off, copy-and-verify migration of local media ledgers/assets into D1/R2.

This command never removes local files. A row is reported eligible for manual cleanup
only after the R2 object is fetched and hash-checked and D1 reads back the imported state.
"""
import argparse
import datetime
import hashlib
import http.client
import json
import sqlite3
import ssl
import sys
import urllib.error
import uuid
from pathlib import Path

from media_online import Cloudflare

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts' / 'favicon'), str(ROOT / 'scripts' / 'thumbnail')]
import favicon
import thumbnail

# Cloudflareクライアントが投げる通信系の例外だけ行単位で握る。ローカル読み取り失敗や
# 不正データ(ValueError等)は従来どおり実行全体を止める。
TRANSPORT_ERRORS = (RuntimeError, urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException, ssl.SSLError)


def import_ledger(client, kind, sources, apply=False, progress=None):
    if kind not in ('favicon', 'thumbnail'):
        raise ValueError('kind must be favicon or thumbnail')
    table = 'favicons' if kind == 'favicon' else 'thumbs'
    prefix = 'icon/' if kind == 'favicon' else 'thumb/'
    outcomes = {'verified': [], 'ineligible': []}
    # Select one newest legacy row per site before any writes. Ties with conflicting bytes
    # are ambiguous and must be reviewed instead of overwriting the fixed R2 key.
    selected = {}
    for db_path, asset_dir in sources:
        db = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            for row_obj in db.execute(f'SELECT * FROM {table} ORDER BY site_id'):
                row = dict(row_obj)
                sid = row['site_id']
                stamp = row.get('captured_at' if kind == 'thumbnail' else 'changed_at') or row.get('last_checked_at') or ''
                prior = selected.get(sid)
                if prior:
                    if stamp > prior[0]:
                        selected[sid] = (stamp, row, Path(asset_dir))
                    elif stamp == prior[0] and (prior[1].get('conflict') or row.get('thumb_hash' if kind == 'thumbnail' else 'icon_hash') != prior[1].get('thumb_hash' if kind == 'thumbnail' else 'icon_hash')):
                        selected[sid] = (stamp, {'site_id': sid, 'conflict': True}, Path(asset_dir))
                else:
                    selected[sid] = (stamp, row, Path(asset_dir))
        finally:
            db.close()
    selected_rows = []
    for sid, value in selected.items():
        if value[1].get('conflict'):
            outcomes['ineligible'].append({'site_id': sid, 'reason': 'same-time local ledgers disagree'})
        else:
            selected_rows.append((sid, value))
    prefetched = {}
    # Read site projections and current media state in bounded D1 batches.
    for start in range(0, len(selected_rows), 20):
        chunk = selected_rows[start:start + 20]
        statements = []
        for sid, _ in chunk:
            statements.extend((
                ('SELECT progress_key,favicon_path,thumb_path FROM sites WHERE id=?', (sid,)),
                ('SELECT state,content_hash,asset_key,metadata_json,updated_at FROM site_media_jobs WHERE site_id=? AND kind=?', (sid, kind)),
            ))
        results = client.batch(statements)
        for i, (sid, _) in enumerate(chunk):
            prefetched[sid] = ((results[i * 2] or [None])[0], (results[i * 2 + 1] or [None])[0])
    pending_writes = []
    owner = str(uuid.uuid4())
    active_leases = set()

    def release_owned_lease(sid):
        try:
            client.query('UPDATE site_media_jobs SET lease_owner=NULL,lease_until=NULL WHERE site_id=? AND kind=? AND lease_owner=?',
                         (sid, kind, owner))
            active_leases.discard(sid)
        except Exception:
            # Never hide the original transport/database error; the existing lease expires.
            pass

    def owned_call(sid, method, *args, **kwargs):
        try:
            return getattr(client, method)(*args, **kwargs)
        except BaseException:
            if apply:
                release_owned_lease(sid)
            raise

    try:
        for index, (sid, (stamp, row, asset_dir)) in enumerate(selected_rows, 1):
                try:
                    if progress and index % 100 == 0:
                        progress({'kind': kind, 'processed': index, 'total': len(selected_rows),
                                  'api_requests': getattr(client, 'request_count', None),
                                  'retries': getattr(client, 'retry_count', None),
                                  'rate_limits': getattr(client, 'rate_limit_count', None)})
                    sid = row['site_id']
                    site, current = prefetched[sid]
                    if not site or not site.get('progress_key'):
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'site/progress identity missing'})
                        continue
                    if not current:
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 media job missing'})
                        continue
                    current_meta = json.loads(current.get('metadata_json') or '{}')
                    current_stamp = current_meta.get('captured_at' if kind == 'thumbnail' else 'changed_at') or current_meta.get('last_checked_at') or ''
                    if current_stamp and stamp and current_stamp > stamp:
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 has a newer media result'})
                        continue
                    if current_stamp == stamp and current.get('content_hash') and row.get('thumb_hash' if kind == 'thumbnail' else 'icon_hash') != current['content_hash']:
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'same-time D1 and local assets disagree'})
                        continue
                    content_hash = row.get('icon_hash' if kind == 'favicon' else 'thumb_hash')
                    exclude = bool(row.get('exclude', 0))
                    state = 'excluded' if exclude else ('ok' if content_hash else ('none' if row.get('status') == 'none' else 'failed' if row.get('status') not in ('pending', None) else 'pending'))
                    fixed_key = favicon.site_name(sid) if kind == 'favicon' else thumbnail.thumb_name(sid)
                    pointer = site['favicon_path' if kind == 'favicon' else 'thumb_path']
                    if not content_hash and not exclude and pointer:
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 already points to an asset absent from this local row'})
                        continue
                    if exclude and (content_hash or pointer or current.get('asset_key')):
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'excluded image has existing local or online media; refusing pointer changes'})
                        continue
                    lease_until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)).isoformat(timespec='seconds').replace('+00:00', 'Z')
                    if apply:
                        lease = owned_call(sid, 'query',
                            'UPDATE site_media_jobs SET lease_owner=?,lease_until=? WHERE identity_key=? AND site_id=? AND kind=? AND (lease_until IS NULL OR julianday(lease_until)<=julianday(\'now\')) RETURNING site_id',
                            (owner, lease_until, site['progress_key'], sid, kind))
                        if not lease:
                            outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 media job is currently leased'})
                            continue
                        active_leases.add(sid)
                    key_name = fixed_key if content_hash and not exclude else None
                    asset_key = key_name
                    action = 'state-only'
                    if exclude:
                        remote = owned_call(sid, 'get_r2', prefix + fixed_key) if apply else client.get_r2(prefix + fixed_key)
                        if remote is not None:
                            if apply:
                                release_owned_lease(sid)
                            outcomes['ineligible'].append({'site_id': sid, 'reason': 'R2 asset exists for excluded site; refusing cleanup eligibility'})
                            continue
                        action = 'excluded-no-asset'
                    if content_hash and not exclude:
                        local_path = Path(asset_dir) / key_name
                        try:
                            data = local_path.read_bytes() if local_path.is_file() else None
                        except BaseException:
                            if apply: release_owned_lease(sid)
                            raise
                        remote = owned_call(sid, 'get_r2', prefix + key_name) if apply else client.get_r2(prefix + key_name)
                        if data is not None:
                            actual = favicon.icon_name(data) if kind == 'favicon' else thumbnail.content_hash(data)
                            if actual != content_hash:
                                if apply: release_owned_lease(sid)
                                outcomes['ineligible'].append({'site_id': sid, 'reason': 'local asset hash mismatch'})
                                continue
                        if remote is not None:
                            actual = favicon.icon_name(remote) if kind == 'favicon' else thumbnail.content_hash(remote)
                            if actual != content_hash:
                                if apply: release_owned_lease(sid)
                                outcomes['ineligible'].append({'site_id': sid, 'reason': 'existing R2 object differs; refusing overwrite'})
                                continue
                            action = 'verified-existing-r2-object'
                        elif not apply and data is None:
                            outcomes['ineligible'].append({'site_id': sid, 'reason': 'asset missing locally and in R2'})
                            continue
                        elif not apply:
                            # Dry-run can validate the local bytes/hash and report the upload
                            # that --apply would perform without treating the absent remote
                            # object as a failure or attempting a write.
                            remote = None
                            action = 'would-upload-local-object'
                        elif data is None:
                            if apply: release_owned_lease(sid)
                            outcomes['ineligible'].append({'site_id': sid, 'reason': 'asset missing locally and in R2'})
                            continue
                        else:
                            pointer_col = 'favicon_path' if kind == 'favicon' else 'thumb_path'
                            still_owned = owned_call(sid, 'query',
                                f"SELECT j.site_id FROM site_media_jobs j JOIN sites s ON s.id=j.site_id WHERE j.identity_key=? AND j.site_id=? AND j.kind=? AND j.lease_owner=? AND julianday(j.lease_until)>julianday('now','+2 minutes') AND j.content_hash IS ? AND j.asset_key IS ? AND j.updated_at IS ? AND s.{pointer_col} IS ?",
                                (site['progress_key'], sid, kind, owner, current.get('content_hash'), current.get('asset_key'), current.get('updated_at'), pointer))
                            if not still_owned:
                                release_owned_lease(sid)
                                outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 lease or media state changed before R2 upload'})
                                continue
                            def validate_put_attempt():
                                owned = owned_call(sid, 'query',
                                    f"SELECT j.site_id FROM site_media_jobs j JOIN sites s ON s.id=j.site_id WHERE j.identity_key=? AND j.site_id=? AND j.kind=? AND j.lease_owner=? AND julianday(j.lease_until)>julianday('now','+2 minutes') AND j.content_hash IS ? AND j.asset_key IS ? AND j.updated_at IS ? AND s.{pointer_col} IS ?",
                                    (site['progress_key'], sid, kind, owner, current.get('content_hash'), current.get('asset_key'), current.get('updated_at'), pointer))
                                if not owned:
                                    raise RuntimeError('D1 lease/state changed before R2 PUT attempt')
                                existing = owned_call(sid, 'get_r2', prefix + key_name)
                                if existing is not None:
                                    remote_hash = favicon.icon_name(existing) if kind == 'favicon' else thumbnail.content_hash(existing)
                                    if remote_hash != content_hash:
                                        raise RuntimeError('R2 object changed before R2 PUT attempt; refusing overwrite')
                                # Recheck after the R2 read, which may take time or observe a competing writer.
                                owned = owned_call(sid, 'query',
                                    f"SELECT j.site_id FROM site_media_jobs j JOIN sites s ON s.id=j.site_id WHERE j.identity_key=? AND j.site_id=? AND j.kind=? AND j.lease_owner=? AND julianday(j.lease_until)>julianday('now','+2 minutes') AND j.content_hash IS ? AND j.asset_key IS ? AND j.updated_at IS ? AND s.{pointer_col} IS ?",
                                    (site['progress_key'], sid, kind, owner, current.get('content_hash'), current.get('asset_key'), current.get('updated_at'), pointer))
                                if not owned:
                                    raise RuntimeError('D1 lease/state changed before R2 PUT attempt')
                            owned_call(sid, 'put_r2', prefix + key_name, data, before_attempt=validate_put_attempt)
                            remote = owned_call(sid, 'get_r2', prefix + key_name)
                            action = 'uploaded-and-readback-verified'
                        if apply and remote is None:
                            release_owned_lease(sid)
                            outcomes['ineligible'].append({'site_id': sid, 'reason': 'R2 readback failed after upload'})
                            continue
                        if remote is not None:
                            actual = favicon.icon_name(remote) if kind == 'favicon' else thumbnail.content_hash(remote)
                            if actual != content_hash:
                                if apply: release_owned_lease(sid)
                                outcomes['ineligible'].append({'site_id': sid, 'reason': 'remote R2 asset hash mismatch'})
                                continue
                    meta = {k: v for k, v in row.items() if k != 'site_id'}
                    checked = row.get('last_checked_at')
                    retry = row.get('next_check_at')
                    error = row.get('last_error') or (row.get('status') if state == 'failed' else None)
                    failures = int(row.get('consecutive_failures') or 0)
                    if not apply:
                        outcomes['verified'].append({'site_id': sid, 'eligible_for_cleanup': False, 'would_import': True, 'action': action})
                        continue
                    metadata_json = json.dumps(meta, ensure_ascii=False, separators=(',', ':'))
                    if kind == 'favicon':
                        site_sql = 'UPDATE sites SET favicon_path=?,favicon_v=? WHERE id=?'
                        site_params = (key_name, favicon.icon_v(content_hash) if content_hash else None, sid)
                    else:
                        site_sql = 'UPDATE sites SET thumb_path=?,thumb_lqip=?,thumb_captured_at=? WHERE id=?'
                        site_params = (key_name, row.get('lqip') if content_hash else None, row.get('captured_at') if content_hash else None, sid)
                    job_sql = ('UPDATE site_media_jobs SET state=?,attempts=?,consecutive_failures=?,last_error=?,last_checked_at=?,next_retry_at=?,excluded=?,content_hash=?,asset_key=?,metadata_json=?,updated_at=datetime(\'now\') '
                               'WHERE identity_key=? AND site_id=? AND kind=? AND lease_owner=? AND julianday(lease_until)>julianday(\'now\') AND content_hash IS ? AND asset_key IS ? AND updated_at IS ? RETURNING site_id')
                    job_params = (state, int(row.get('attempts') or failures), failures, error, checked, retry, int(exclude), content_hash, asset_key,
                                  metadata_json, site['progress_key'], sid, kind, owner, current.get('content_hash'), current.get('asset_key'), current.get('updated_at'))
                    if exclude:
                        site_sql, site_params = ('SELECT id FROM sites WHERE id=? AND ' + ('favicon_path' if kind == 'favicon' else 'thumb_path') + ' IS NULL', (sid,))
                    elif kind == 'favicon':
                        site_sql = "UPDATE sites SET favicon_path=?,favicon_v=? WHERE id=? AND favicon_path IS ? AND EXISTS (SELECT 1 FROM site_media_jobs WHERE site_id=? AND kind=? AND lease_owner=? AND julianday(lease_until)>julianday('now') AND state=? AND content_hash IS ? AND asset_key IS ?) RETURNING id"
                        site_params = (key_name, favicon.icon_v(content_hash) if content_hash else None, sid, pointer,
                                       sid, kind, owner, state, content_hash, asset_key)
                    else:
                        site_sql = "UPDATE sites SET thumb_path=?,thumb_lqip=?,thumb_captured_at=? WHERE id=? AND thumb_path IS ? AND EXISTS (SELECT 1 FROM site_media_jobs WHERE site_id=? AND kind=? AND lease_owner=? AND julianday(lease_until)>julianday('now') AND state=? AND content_hash IS ? AND asset_key IS ?) RETURNING id"
                        site_params = (key_name, row.get('lqip') if content_hash else None, row.get('captured_at') if content_hash else None,
                                       sid, pointer, sid, kind, owner, state, content_hash, asset_key)
                    verify_sql = 'SELECT state,content_hash,asset_key,metadata_json,lease_owner FROM site_media_jobs WHERE site_id=? AND kind=?'
                    pointer_sql = f"SELECT {'favicon_path' if kind == 'favicon' else 'thumb_path'} AS pointer FROM sites WHERE id=?"
                    pending_writes.append((sid, state, content_hash, asset_key, metadata_json, pointer,
                                           (job_sql, job_params), (site_sql, site_params), (verify_sql, (sid, kind)), (pointer_sql, (sid,)),
                                           ('UPDATE site_media_jobs SET lease_owner=NULL,lease_until=NULL WHERE site_id=? AND kind=? AND lease_owner=?', (sid, kind, owner))))
                    item = pending_writes.pop()
                    statements = [item[6], item[7], item[8], item[9], item[10]]
                    results = owned_call(sid, 'batch', statements)
                    active_leases.discard(sid)
                    sid, state, content_hash, asset_key, metadata_json, prior_pointer = item[:6]
                    job_written = (results[0] or [None])[0]
                    site_written = (results[1] or [None])[0]
                    verify = (results[2] or [None])[0]
                    pointer_row = (results[3] or [None])[0]
                    expected_pointer = asset_key if content_hash and not exclude else prior_pointer
                    if (not job_written or not site_written or not verify or not pointer_row or pointer_row.get('pointer') != expected_pointer or
                            verify.get('state') != state or verify.get('content_hash') != content_hash or verify.get('asset_key') != asset_key or
                            verify.get('metadata_json') != metadata_json or verify.get('lease_owner') != owner):
                        outcomes['ineligible'].append({'site_id': sid, 'reason': 'D1 guarded update/readback mismatch'})
                        continue
                    outcomes['verified'].append({'site_id': sid, 'eligible_for_cleanup': True, 'action': action})
                except TRANSPORT_ERRORS as exc:  # per-row R2/D1 transport failure must not abort the run
                    if apply:
                        release_owned_lease(sid)
                    short = ' '.join(str(exc).split())[:120] or type(exc).__name__
                    outcomes['ineligible'].append({'site_id': sid, 'reason': f'R2/D1 transport error: {short}'})
                    continue
    finally:
        for leased_sid in tuple(active_leases):
            release_owned_lease(leased_sid)
    return outcomes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--kind', required=True, choices=('favicon', 'thumbnail'))
    ap.add_argument('--source', action='append', required=True, metavar='DB=ASSET_DIR',
                    help='repeat for each legacy ledger and its matching local asset directory')
    ap.add_argument('--apply', action='store_true', help='upload assets and write D1 (default: read-only dry run)')
    a = ap.parse_args(argv)
    sources = []
    for spec in a.source:
        db, sep, asset_dir = spec.partition('=')
        if not sep or not db or not asset_dir:
            ap.error('--source must be DB=ASSET_DIR')
        sources.append((Path(db), Path(asset_dir)))
    # The importer is deliberately serial and capped at two API requests per second.
    # A bounded client-side rate also applies to D1 batches and R2 readbacks.
    client = Cloudflare(min_request_interval=0.5, max_concurrent=1, max_attempts=7)
    result = import_ledger(client, a.kind, sources, apply=a.apply,
                           progress=lambda item: print(json.dumps({'progress': item}, ensure_ascii=False), file=sys.stderr, flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['ineligible']))


if __name__ == '__main__':
    raise SystemExit(main())
