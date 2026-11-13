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
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='frontier'").fetchone()[0]
    if "'deferred'" not in sql:  # 旧台帳の state 制約に deferred を追加（表を作り直す）
        db.executescript('ALTER TABLE frontier RENAME TO frontier_old;')
        db.executescript((Path(__file__).parent / 'schema.sql').read_text(encoding='utf-8').replace('CREATE INDEX IF NOT EXISTS frontier_state_next', 'DROP INDEX IF EXISTS frontier_state_next; CREATE INDEX frontier_state_next'))
        old = [r[1] for r in db.execute('PRAGMA table_info(frontier_old)')]
        new = [r[1] for r in db.execute('PRAGMA table_info(frontier)')]
        shared = ', '.join(c for c in old if c in new)
        db.execute(f'INSERT INTO frontier ({shared}) SELECT {shared} FROM frontier_old')
        db.execute('DROP TABLE frontier_old'); db.commit()
    cols = [r[1] for r in db.execute('PRAGMA table_info(frontier)')]
    for c in ('site_kind', 'adult'):
        if c not in cols:
            db.execute(f'ALTER TABLE frontier ADD COLUMN {c} TEXT')  # 旧台帳の追従
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
