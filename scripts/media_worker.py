"""Consume successful shipped cycles on the Windows host, including previous cycles."""
import argparse
import contextlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import nightly_media as media


def collect(directory, db):
    """Only completed, successfully applied production cycles can enqueue work."""
    db.execute('CREATE TABLE IF NOT EXISTS media_cycles (name TEXT PRIMARY KEY)')
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or db.execute('SELECT 1 FROM media_cycles WHERE name=?', (path.name,)).fetchone():
            continue
        try:
            status = json.loads((path / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue  # a currently running cycle has no final status yet
        prod = next((s for s in status.get('stages', []) if s.get('name') == 'prod'), {})
        if status.get('ok') is not True or not status.get('finished_at') or status.get('prod_applied') is not True or prod.get('status') != 'ok':
            continue
        try:
            media.enqueue(db, path / 'keys.txt', path / 'export.sqlite')
        except (OSError, ValueError, sqlite3.Error) as e:
            print('cannot enqueue', path.name, str(e), flush=True)
            continue  # retry missing/incomplete exports; do not starve other cycles
        db.execute('INSERT INTO media_cycles(name) VALUES(?)', (path.name,))
        db.commit()
        print('queued', path.name, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--var', type=Path, required=True)
    ap.add_argument('--wsl-distro')
    ap.add_argument('--wsl-playwright')
    ap.add_argument('--wsl-chromium')
    ap.add_argument('--favicon-limit', type=int, default=100)
    ap.add_argument('--thumbnail-limit', type=int, default=10)
    ap.add_argument('--interval', type=int, default=300)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args(argv)
    if any((a.wsl_distro, a.wsl_playwright, a.wsl_chromium)):
        ap.error('WSL オプションは利用できません。Windows ネイティブで撮影します')
    if sys.platform != 'win32' or not 1 <= a.favicon_limit <= 100 or not 0 <= a.thumbnail_limit <= 100 or a.interval < 60:
        ap.error('Windows 専用。ファビコン上限は 1–100、サムネイルは 0–100、間隔は 60 秒以上')
    root = a.var / 'nightly'
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.media-worker.lock').open('a+') as worker_lock:
        media.filelock.lock(worker_lock, blocking=False)
        while True:
            rc = 1
            with (root / '.media.lock').open('a+') as lock:
                media.filelock.lock(lock)
                with contextlib.closing(sqlite3.connect(root / 'media.sqlite')) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS pending (kind TEXT, site_id TEXT, retry_at TEXT NOT NULL, PRIMARY KEY(kind,site_id))')
                    collect(root, db)
            try:
                cmd = ['--var', str(a.var), '--out', str(root), '--d1-name', 'mise',
                       '--favicon-limit', str(a.favicon_limit), '--thumbnail-limit', str(a.thumbnail_limit)]
                rc = media.main(cmd)
                if rc:
                    print('画像処理に失敗。再試行期限を確認', flush=True)
            except subprocess.TimeoutExpired as e:
                print('画像処理がタイムアウト。次の実行で再試行:', e, flush=True)
            except OSError as e:
                print('画像処理を待って再試行:', e, flush=True)
            if a.once:
                return rc
            time.sleep(a.interval)


if __name__ == '__main__':
    sys.exit(main())
