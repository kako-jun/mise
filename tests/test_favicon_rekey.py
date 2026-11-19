"""ファビコンの R2 キー移行(#163: scripts/favicon/rekey.py)と、サイト固定キー化・delete の台帳まわり。
wrangler・ネットワークは呼ばない(R2Store の runner を差し替える)。実行: uv run --with pillow python3 -m unittest tests.test_favicon_rekey"""
import contextlib
import hashlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'favicon'))
import favicon
import rekey
from PIL import Image


def webp(color):
    b = io.BytesIO(); Image.new('RGBA', (32, 32), color).save(b, 'WEBP', lossless=True); return b.getvalue()


def make_legacy(path, rows, stored=()):
    """旧スキーマの台帳(内容ハッシュ名の icon_stored のみ。exclude 列なし)。"""
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE favicons (site_id TEXT PRIMARY KEY, url TEXT NOT NULL, icon_url TEXT, etag TEXT, last_modified TEXT, icon_hash TEXT, status TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT, next_check_at TEXT, changed_at TEXT)')
    c.execute('CREATE TABLE icon_stored (name TEXT NOT NULL, store TEXT NOT NULL, PRIMARY KEY (name, store))')
    for sid, h, st, chk in rows:
        c.execute('INSERT INTO favicons(site_id,url,icon_hash,status,last_checked_at) VALUES(?,?,?,?,?)', (sid, 'https://' + sid + '.example/', h, st, chk))
    for n, s in stored:
        c.execute('INSERT INTO icon_stored VALUES(?,?)', (n, s))
    c.commit(); c.close()


class Rekey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.src = t / 'src'; self.src.mkdir()
        self.red, self.blue = webp((200, 0, 0, 255)), webp((0, 0, 200, 255))
        self.hr, self.hb = favicon.icon_name(self.red), favicon.icon_name(self.blue)
        (self.src / self.hr).write_bytes(self.red); (self.src / self.hb).write_bytes(self.blue)
        self.stale = 'e' * 32 + '.webp'   # 台帳に載らない過去の旧名(R2 に残っている孤児)
        # 台帳 2 本(分割した台帳)。s1,s2 は同じ画像(旧名は共有)、s3 は別画像、s4 は失敗(対象外)、s5 は画像ファイルが無い
        self.d1, self.d2 = t / 'a.sqlite', t / 'b.sqlite'
        make_legacy(self.d1, [('s1', self.hr, 'ok', '2026-11-01'), ('s2', self.hr, 'ok', '2026-11-01'), ('s4', None, 'http_404', None)], [(self.hr, 'r2-remote'), (self.stale, 'r2-remote'), (self.hr, 'local')])
        make_legacy(self.d2, [('s3', self.hb, 'ok', '2026-11-02'), ('s5', 'd' * 32 + '.webp', 'ok', '2026-11-02')], [(self.hb, 'r2-remote')])
        self.calls = []; self.lock = threading.Lock(); self.fail = set()
        self.dbs = [str(self.d1), str(self.d2)]
        self.dbarg = ['--db', self.dbs[0], '--db', self.dbs[1]]

    def runner(self, args):
        with self.lock:
            self.calls.append(list(args))
        if args[:3] == ['r2', 'object', 'put'] and any(args[3].endswith(f) for f in self.fail):
            return 1
        return 0

    def store(self, **kw):
        return favicon.R2Store(remote=True, runner=self.runner, verify=True, **kw)

    def puts(self):
        return sorted(c[3] for c in self.calls if c[:3] == ['r2', 'object', 'put'])

    def test_collect_and_plan(self):
        items, dups, old = rekey.collect(self.dbs)
        self.assertEqual(sorted(items), ['s1', 's2', 's3', 's5'])
        self.assertEqual(items['s1'].name, favicon.site_name('s1'))
        self.assertNotEqual(items['s1'].name, items['s2'].name)   # 同じ内容でも別サイトは別キー
        self.assertEqual(items['s1'].v, self.hr[:8])
        self.assertEqual(old, {self.hr, self.hb, 'd' * 32 + '.webp', self.stale})
        self.assertEqual(dups, 0)
        for d in self.dbs:   # 読み取りだけ(旧台帳に表が増えない)
            c = sqlite3.connect(d); self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='icon_put'").fetchone()[0], 0); c.close()

    def test_duplicate_site_in_two_ledgers_takes_latest(self):
        make_legacy(self.d2.parent / 'c.sqlite', [('s1', self.hb, 'ok', '2026-11-09')])
        items, dups, _ = rekey.collect(self.dbs + [str(self.d2.parent / 'c.sqlite')])
        self.assertEqual((items['s1'].content, dups), (self.hb, 1))

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        items, _, _ = rekey.collect(self.dbs)
        st = rekey.upload(items, self.store(), self.src, self.src, apply=False, log=lambda m: None)
        self.assertEqual((st['todo'], st['missing_source']), (3, 1))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.src / favicon.site_name('s1')).exists())

    def test_upload_places_each_site_at_its_own_key_marks_and_is_resumable(self):
        items, _, _ = rekey.collect(self.dbs)
        st = rekey.upload(items, self.store(), self.src, self.src, apply=True, jobs=4, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['failed'], st['missing_source']), (3, 0, 1))
        self.assertEqual(self.puts(), sorted(f'mise-media/icon/{favicon.site_name(s)}' for s in ('s1', 's2', 's3')))
        self.assertTrue(all('--remote' in c and 'image/webp' in c for c in self.calls))
        self.assertEqual((self.src / favicon.site_name('s1')).read_bytes(), self.red)   # 手元にも新キーで写す
        db = favicon.open_readonly(self.d1)
        self.assertTrue(favicon._marked(db, 's1', 'r2-remote', self.hr)); self.assertTrue(favicon._marked(db, 's1', 'local', self.hr)); db.close()
        self.calls.clear()   # 再実行: 印があるので何も置かない
        st = rekey.upload(items, self.store(), self.src, self.src, apply=True, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['skipped_marked']), (0, 3)); self.assertEqual(self.puts(), [])

    def test_failure_is_retried_then_counted_and_next_run_finishes(self):
        items, _, _ = rekey.collect(self.dbs)
        self.fail = {favicon.site_name('s2')}
        st = rekey.upload(items, self.store(), self.src, None, apply=True, retries=2, sleep=lambda s: None, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['failed']), (2, 1))
        self.assertEqual(self.puts().count(f'mise-media/icon/{favicon.site_name("s2")}'), 3)   # 1 回 + 再試行 2 回
        self.fail = set(); self.calls.clear()
        st = rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['skipped_marked']), (1, 2)); self.assertEqual(self.puts(), [f'mise-media/icon/{favicon.site_name("s2")}'])

    def test_broken_source_is_not_uploaded(self):
        (self.src / self.hb).write_bytes(self.red)   # 名前と中身が合わない
        items, _, _ = rekey.collect(self.dbs)
        st = rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.assertEqual((st['bad_source'], st['uploaded']), (1, 2))

    def test_sql_only_for_placed_sites_and_idempotent_shape(self):
        items, _, _ = rekey.collect(self.dbs)
        self.assertEqual(rekey.sql_text(items), '')   # まだ何も置いていない
        self.fail = {favicon.site_name('s3')}
        rekey.upload(items, self.store(), self.src, None, apply=True, retries=0, log=lambda m: None)
        text = rekey.sql_text(items)
        self.assertEqual(len(text.splitlines()), 2)
        v = self.hr[:8]
        self.assertIn(f"UPDATE sites SET favicon_path='{favicon.site_name('s1')}', favicon_v='{v}' WHERE id='s1' AND favicon_v IS NOT '{v}';", text)
        self.assertNotIn("'s3'", text); self.assertNotIn("'s5'", text)
        self.assertEqual(rekey.sql_text(items, 'r2-local'), '')   # 別の保存先の印は混ざらない

    def test_delete_old_requires_placed_new_keys_and_only_then_deletes(self):
        items, _, old = rekey.collect(self.dbs)
        self.calls.clear()
        st = rekey.delete_old(items, old, self.store(), 'r2-remote', apply=True, log=lambda m: None)
        self.assertEqual((st['deleted'], st['kept_unplaced']), (1, 3))   # 参照サイトが無い孤児(stale)だけ。他は新キー未配置で残す
        self.assertEqual([c[3] for c in self.calls], [f'mise-media/icon/{self.stale}'])
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.calls.clear()
        st = rekey.delete_old(items, old, self.store(), 'r2-remote', apply=True, log=lambda m: None)
        self.assertEqual((st['deleted'], st['kept_unplaced']), (3, 1))   # hr(s1,s2 とも配置済み)・hb・stale。s5 は元画像が無く未配置なので、その旧名は残る
        self.assertEqual(sorted(c[3] for c in self.calls), sorted(f'mise-media/icon/{n}' for n in (self.hr, self.hb, self.stale)))

    def test_delete_old_keeps_name_while_any_referrer_unplaced(self):
        items, _, old = rekey.collect(self.dbs)
        self.fail = {favicon.site_name('s2')}
        rekey.upload(items, self.store(), self.src, None, apply=True, retries=0, log=lambda m: None)
        self.calls.clear()
        rekey.delete_old(items, old, self.store(), 'r2-remote', apply=True, log=lambda m: None)
        deleted = sorted(c[3] for c in self.calls if c[:3] == ['r2', 'object', 'delete'])
        self.assertEqual(deleted, sorted(f'mise-media/icon/{n}' for n in (self.hb, self.stale)))   # hr は s2 が未配置なので残る・s5 の旧名も残る

    def test_delete_old_dry_run_and_verify(self):
        items, _, old = rekey.collect(self.dbs)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.calls.clear()
        rekey.delete_old(items, old, self.store(), 'r2-remote', apply=False, log=lambda m: None)
        self.assertEqual(self.calls, [])
        rekey.delete_old(items, old, self.store(), 'r2-remote', verify=True, apply=False, log=lambda m: None)
        self.assertTrue(all(c[:3] == ['r2', 'object', 'get'] for c in self.calls) and self.calls)   # 新キーの実在を確かめるだけ

    def test_new_key_names_are_never_listed_as_old(self):
        make_legacy(self.d2.parent / 'k.sqlite', [('s9', favicon.site_name('s9'), 'ok', '2026-11-01')])   # 偶然、新キーと同じ名前が旧名にある
        items, _, old = rekey.collect(self.dbs + [str(self.d2.parent / 'k.sqlite')])
        self.assertNotIn(favicon.site_name('s9'), old)

    def test_cli_guards(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rekey.main([*self.dbarg, '--store-dir', str(self.src), 'delete-old', '--apply']), 2)   # --d1-done が無い
            self.assertEqual(rekey.main([*self.dbarg, '--store-dir', str(self.src), 'plan']), 0)
        self.assertIn('--d1-done', err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rekey.main([*self.dbarg, 'list-old'])
        self.assertIn(f'icon/{self.stale}', out.getvalue())


class FixedKeyLedger(unittest.TestCase):
    """favicon.py 側: 内容が変わったときだけ sql を出す・delete・旧台帳の移行。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.db = favicon.connect(':memory:')
        favicon.import_sites(self.db, [('s1', 'https://a.example/')])

    def runner(self, args):
        self.calls.append(list(args)); return 0

    def row(self):
        return self.db.execute("SELECT * FROM favicons WHERE site_id='s1'").fetchone()

    def res(self, color):
        return {'status': 'ok', 'icon_url': 'https://x.example/i.png', 'etag': None, 'webp': webp(color)}

    def test_r2_overwrites_same_key_when_content_changes_and_sql_follows(self):
        import datetime
        T0 = datetime.datetime(2026, 11, 20, tzinfo=datetime.timezone.utc)
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((1, 2, 3, 255)), T0)
        v1 = favicon.icon_v(self.row()['icon_hash'])
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((1, 2, 3, 255)), T0)   # 同じ内容: 置かない
        self.assertEqual(len(self.calls), 1)
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((9, 9, 9, 255)), T0)   # 変わった: 同じキーに上書き
        self.assertEqual(len(self.calls), 2); self.assertEqual(self.calls[0][3], self.calls[1][3])
        self.assertTrue(self.calls[1][3].endswith('icon/' + favicon.site_name('s1')))
        v2 = favicon.icon_v(self.row()['icon_hash']); self.assertNotEqual(v1, v2)
        self.assertIn(f"favicon_v='{v2}'", favicon.sql_updates(self.db, store='r2-remote'))
        self.assertIn(f"AND favicon_v IS NOT '{v2}'", favicon.sql_updates(self.db, store='r2-remote'))

    def test_delete_dry_run_apply_and_null_update(self):
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((1, 2, 3, 255)), datetime_now())
        self.calls.clear()
        st = favicon.R2Store(remote=True, runner=self.runner)
        self.assertEqual(favicon.delete_sites(self.db, st, ['s1'], apply=False, log=lambda m: None), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(favicon.delete_sites(self.db, st, ['s1'], apply=True, log=lambda m: None), 1)
        self.assertEqual(self.calls, [['r2', 'object', 'delete', f'mise-media/icon/{favicon.site_name("s1")}', '--remote']])
        r = self.row(); self.assertEqual((r['exclude'], r['icon_hash'], r['status']), (1, None, 'excluded'))
        self.assertFalse(favicon._marked(self.db, 's1', 'r2-remote'))
        sql = favicon.sql_updates(self.db)
        self.assertEqual(sql, "UPDATE sites SET favicon_path=NULL, favicon_v=NULL WHERE id='s1' AND (favicon_path IS NOT NULL OR favicon_v IS NOT NULL);\n")
        self.assertEqual(favicon.due_sites(self.db, datetime_now() + __import__('datetime').timedelta(days=99), 10), [])   # 除外は取りに行かない

    def test_delete_failure_keeps_ledger(self):
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((1, 2, 3, 255)), datetime_now())
        with self.assertRaises(OSError):
            favicon.delete_sites(self.db, favicon.R2Store(remote=True, runner=lambda a: 1), ['s1'], apply=True, log=lambda m: None)
        self.assertEqual(self.row()['exclude'], 0); self.assertIsNotNone(self.row()['icon_hash'])

    def test_cli_delete_guards(self):
        t = Path(self.tmp.name) / 'f.sqlite'
        db = favicon.connect(t); favicon.import_sites(db, [('s1', 'https://a.example/')]); db.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(favicon.main(['--db', str(t), 'delete', '--site', 's1', '--apply']), 2)   # --d1-done が無いと R2 を消さない
            self.assertEqual(favicon.main(['--db', str(t), 'delete', '--site', 's1']), 0)   # 既定 dry-run
        self.assertIn('--d1-done', err.getvalue()); self.assertIn('dry-run', out.getvalue())
        self.assertEqual(sqlite3.connect(t).execute('SELECT exclude FROM favicons').fetchone()[0], 0)

    def test_old_schema_ledger_commands_do_not_crash_readonly_and_migrate_on_connect(self):
        t = Path(self.tmp.name)
        p = t / 'old.sqlite'; make_legacy(p, [('s1', 'a' * 32 + '.webp', 'ok', '2026-11-01')], [('a' * 32 + '.webp', 'r2-remote')])
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as out:
            for argv in (['sql'], ['sql', '--store', 'local'], ['status'], ['run'], ['delete', '--site', 's1']):
                self.assertEqual(favicon.main(['--db', str(p), *argv]), 0, argv)
        self.assertEqual(sqlite3.connect(p).execute("SELECT COUNT(*) FROM sqlite_master WHERE name='icon_put'").fetchone()[0], 0)   # 読み取りは何も足さない
        ro = favicon.open_readonly(p)
        sql = favicon.sql_updates(ro)   # 印を問わない全件は旧台帳でも出る(新キー・favicon_v で)
        self.assertIn(f"favicon_path='{favicon.site_name('s1')}', favicon_v='aaaaaaaa'", sql); ro.close()
        db = favicon.connect(p)   # 書き込みで開くと新しい表と列が自動で足される。旧 icon_stored は新キーの印として引き継がない
        self.assertEqual(db.execute('SELECT COUNT(*) FROM icon_put').fetchone()[0], 0)
        self.assertIn('exclude', [r[1] for r in db.execute('PRAGMA table_info(favicons)')])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM icon_stored').fetchone()[0], 1)   # 旧い表は消さない
        self.assertEqual(favicon.sql_updates(db, store='r2-remote'), '')
        db.close()


def datetime_now():
    import datetime
    return datetime.datetime(2026, 11, 20, tzinfo=datetime.timezone.utc)


if __name__ == '__main__':
    unittest.main()
