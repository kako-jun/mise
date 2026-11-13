"""frontier 台帳（SQLite）の薄いラッパー。"""
import datetime
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'var' / 'frontier.sqlite'


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def connect(path=None):
    path = Path(path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript((Path(__file__).parent / 'schema.sql').read_text(encoding='utf-8'))
    return db


def add(db, key, host, url, source, source_detail=None, depth=0, state='discovered', priority=0, reject_reason=None):
    """既存キーは触らない（重複検査）。新規なら True。"""
    cur = db.execute(
        'INSERT OR IGNORE INTO frontier (key,host,url,source,source_detail,discovered_at,depth,state,priority,reject_reason,next_fetch_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (key, host, url, source, source_detail, now(), depth, state, priority, reject_reason, now() if state == 'queued' else None))
    return cur.rowcount == 1


def set_cursor(db, source, key, value):
    db.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?,?,?)', (source, key, str(value), now()))


def get_cursor(db, source, key, default=None):
    r = db.execute('SELECT cursor_value FROM source_cursor WHERE source=? AND cursor_key=?', (source, key)).fetchone()
    return r[0] if r else default
