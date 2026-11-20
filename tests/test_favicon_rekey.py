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
        self.calls = []; self.lock = threading.Lock(); self.fail_names = set()
        self.dbs = [str(self.d1), str(self.d2)]
        self.dbarg = ['--db', self.dbs[0], '--db', self.dbs[1]]

    def runner(self, args):
        with self.lock:
            self.calls.append(list(args))
        if args[:3] == ['r2', 'object', 'put'] and any(args[3].endswith(f) for f in self.fail_names):
            return 1
        return 0

    def store(self, **kw):
        return favicon.R2Store(remote=True, runner=self.runner, verify=True, **kw)

    def puts(self):
        return sorted(c[3] for c in self.calls if c[:3] == ['r2', 'object', 'put'])

    def test_collect_and_plan(self):
        items, dups, old, _ = rekey.collect(self.dbs)
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
        items, dups, _, _ = rekey.collect(self.dbs + [str(self.d2.parent / 'c.sqlite')])
        self.assertEqual((items['s1'].content, dups), (self.hb, 1))

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        items, _, _, _ = rekey.collect(self.dbs)
        st = rekey.upload(items, self.store(), self.src, self.src, apply=False, log=lambda m: None)
        self.assertEqual((st['todo'], st['missing_source']), (3, 1))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.src / favicon.site_name('s1')).exists())

    def test_upload_places_each_site_at_its_own_key_marks_and_is_resumable(self):
        items, _, _, _ = rekey.collect(self.dbs)
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
        items, _, _, _ = rekey.collect(self.dbs)
        self.fail_names = {favicon.site_name('s2')}
        st = rekey.upload(items, self.store(), self.src, None, apply=True, retries=2, sleep=lambda s: None, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['failed']), (2, 1))
        self.assertEqual(self.puts().count(f'mise-media/icon/{favicon.site_name("s2")}'), 3)   # 1 回 + 再試行 2 回
        self.fail_names = set(); self.calls.clear()
        st = rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.assertEqual((st['uploaded'], st['skipped_marked']), (1, 2)); self.assertEqual(self.puts(), [f'mise-media/icon/{favicon.site_name("s2")}'])

    def test_broken_source_is_not_uploaded(self):
        (self.src / self.hb).write_bytes(self.red)   # 名前と中身が合わない
        items, _, _, _ = rekey.collect(self.dbs)
        st = rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.assertEqual((st['bad_source'], st['uploaded']), (1, 2))

    def test_sql_only_for_placed_sites_and_idempotent_shape(self):
        items, _, _, _ = rekey.collect(self.dbs)
        self.assertEqual(rekey.sql_text(items), '')   # まだ何も置いていない
        self.fail_names = {favicon.site_name('s3')}
        rekey.upload(items, self.store(), self.src, None, apply=True, retries=0, log=lambda m: None)
        text = rekey.sql_text(items)
        self.assertEqual(len(text.splitlines()), 2)
        v = self.hr[:8]
        self.assertIn(f"UPDATE sites SET favicon_path='{favicon.site_name('s1')}', favicon_v='{v}' WHERE id='s1' AND (favicon_path IS NOT '{favicon.site_name('s1')}' OR favicon_v IS NOT '{v}');", text)
        self.assertNotIn("'s3'", text); self.assertNotIn("'s5'", text)
        self.assertEqual(rekey.sql_text(items, 'r2-local'), '')   # 別の保存先の印は混ざらない

    def after_switch(self, items):
        """切替済みの D1: 台帳の全サイトが新キー・v 付き。"""
        return [{'id': i.site_id, 'favicon_path': i.name, 'favicon_v': i.v} for i in items.values()]

    def delete(self, items, old, rows, **kw):
        self.calls.clear()
        st = rekey.delete_old(items, old, self.store(), 'r2-remote', rows, apply=True, log=lambda m: None, **kw)
        return st, sorted(c[3] for c in self.calls if c[:3] == ['r2', 'object', 'delete'])

    def test_delete_old_deletes_only_names_d1_no_longer_points_at(self):
        items, _, old, _ = rekey.collect(self.dbs)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        st, deleted = self.delete(items, old, self.after_switch(items))
        self.assertEqual((st['deleted'], st['kept_d1'], st['kept_unplaced']), (3, 0, 1))   # hr・hb・stale。s5 は元画像が無く未配置なので残る
        self.assertEqual(deleted, sorted(f'mise-media/icon/{n}' for n in (self.hr, self.hb, self.stale)))

    def test_delete_old_keeps_names_d1_still_points_at_even_if_ledger_has_no_ok_reference(self):
        """レビュー再現: 以前は取れて今は一時失敗(status=http_503・icon_hash 有り)のサイトを、D1 は旧名のまま指している。"""
        make_legacy(self.d2.parent / 'c.sqlite', [('s6', self.hr, 'http_503', '2026-11-03')])
        dbs = self.dbs + [str(self.d2.parent / 'c.sqlite')]
        items, _, old, _ = rekey.collect(dbs)
        self.assertIn('s6', items)   # status を問わず参照元に数える(upload の対象にも入る)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        rows = self.after_switch(items)
        # D1 が台帳より遅れている: s6 はまだ旧名、s1 もまだ旧名(favicon_v NULL)
        rows = [r for r in rows if r['id'] not in ('s6', 's1')] + [{'id': 's6', 'favicon_path': self.hr, 'favicon_v': None}, {'id': 's1', 'favicon_path': self.hr, 'favicon_v': None}]
        st, deleted = self.delete(items, old, rows)
        self.assertNotIn(f'mise-media/icon/{self.hr}', deleted)
        self.assertEqual(st['kept_d1'], 1)

    def test_delete_old_keeps_stale_name_d1_points_at(self):
        items, _, old, _ = rekey.collect(self.dbs)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        rows = self.after_switch(items) + [{'id': 'ghost', 'favicon_path': self.stale, 'favicon_v': None}]   # どの台帳にも無い名前を D1 だけが指す
        st, deleted = self.delete(items, old, rows)
        self.assertNotIn(f'mise-media/icon/{self.stale}', deleted)

    def test_delete_old_still_requires_new_keys_placed_and_verify_checks_existence(self):
        items, _, old, _ = rekey.collect(self.dbs)
        st, deleted = self.delete(items, old, self.after_switch(items))   # 何も置いていない
        self.assertEqual(deleted, [f'mise-media/icon/{self.stale}'])      # 参照サイトの無い孤児だけ(D1 も指していない)
        self.assertEqual(st['kept_unplaced'], 3)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.calls.clear()
        rekey.delete_old(items, old, self.store(), 'r2-remote', self.after_switch(items), verify=True, apply=False, jobs=3, log=lambda m: None)
        self.assertTrue(self.calls and all(c[:3] == ['r2', 'object', 'get'] for c in self.calls))

    def snapshot(self, rows, wrap=True, age_h=0):
        import json, os, time
        f = Path(self.tmp.name) / 'd1.json'
        f.write_text(json.dumps([{'results': rows, 'success': True}] if wrap else rows))
        t = time.time() - age_h * 3600; os.utime(f, (t, t))
        return str(f)

    def test_snapshot_parsing_and_safety_valves(self):
        items, _, old, _ = rekey.collect(self.dbs)
        rows = self.after_switch(items)
        self.assertEqual(len(rekey.load_d1_snapshot(self.snapshot(rows), len(items))), len(rows))
        self.assertEqual(len(rekey.load_d1_snapshot(self.snapshot(rows, wrap=False), len(items))), len(rows))   # 素の行の配列も可
        for bad, why in ((self.snapshot([]), '空'), (self.snapshot(rows[:1]), '少なすぎる'), (self.snapshot(rows, age_h=48), '古い'), (str(Path(self.tmp.name) / 'none.json'), '無い')):
            with self.assertRaises(rekey.SnapshotError, msg=why):
                rekey.load_d1_snapshot(bad, len(items))
        (Path(self.tmp.name) / 'junk.json').write_text('not json')
        with self.assertRaises(rekey.SnapshotError):
            rekey.load_d1_snapshot(str(Path(self.tmp.name) / 'junk.json'), 1)

    def test_delete_old_cli_requires_snapshot_and_rejects_bad_ones_without_calling_wrangler(self):
        base = [*self.dbarg, '--store-dir', str(self.src), 'delete-old']
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):   # --d1-json 無しは argparse が拒否
                rekey.main(base + ['--apply', '--d1-done'])
            self.assertEqual(rekey.main(base + ['--d1-json', self.snapshot([]), '--apply', '--d1-done']), 2)
            self.assertEqual(rekey.main(base + ['--d1-json', str(Path(self.tmp.name) / 'none.json'), '--apply', '--d1-done']), 2)
            self.assertEqual(rekey.main(base + ['--d1-json', self.snapshot([{'id': 'a', 'favicon_path': 'x', 'favicon_v': None}]), '--apply', '--d1-done']), 2)   # 極端に少ない
            self.assertEqual(rekey.main(base + ['--d1-json', self.snapshot([]), '--apply']), 2)   # --d1-done 無し
        self.assertIn('中止', err.getvalue())

    def test_delete_old_cli_exit_code_nonzero_when_something_is_kept(self):
        items, _, old, _ = rekey.collect(self.dbs)
        f = self.snapshot(self.after_switch(items))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rekey.main([*self.dbarg, '--store-dir', str(self.src), 'delete-old', '--d1-json', f]), 3)   # 未配置で残るものがある(dry-run)

    def test_excluded_site_is_not_uploaded_nor_in_sql_nor_old_deleted_blindly(self):
        c = sqlite3.connect(self.d1); c.execute('ALTER TABLE favicons ADD COLUMN exclude INTEGER NOT NULL DEFAULT 0'); c.execute("UPDATE favicons SET exclude=1 WHERE site_id='s2'"); c.commit(); c.close()
        items, _, old, names = rekey.collect(self.dbs)
        self.assertNotIn('s2', items)
        self.assertIn(favicon.site_name('s2'), names)   # 新キー名は除外サイトのものも守る対象
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        self.assertNotIn(f'mise-media/icon/{favicon.site_name("s2")}', self.puts())
        self.assertNotIn("'s2'", rekey.sql_text(items))

    def test_revert_sql_restores_old_names_and_null_v(self):
        items, _, _, _ = rekey.collect(self.dbs)
        text = rekey.sql_text(items, revert=True)
        self.assertIn(f"UPDATE sites SET favicon_path='{self.hr}', favicon_v=NULL WHERE id='s1' AND favicon_v IS NOT NULL;", text)
        self.assertEqual(len(text.splitlines()), len(items))

    def test_path_only_stale_row_is_fixed_by_sql_condition(self):
        items, _, _, _ = rekey.collect(self.dbs)
        rekey.upload(items, self.store(), self.src, None, apply=True, log=lambda m: None)
        import sqlite3 as q
        m = q.connect(':memory:'); m.execute('CREATE TABLE sites(id TEXT, favicon_path TEXT, favicon_v TEXT)')
        m.execute("INSERT INTO sites VALUES('s1', ?, ?)", (self.hr, self.hr[:8]))   # v は同じで path だけ旧名
        m.executescript(rekey.sql_text(items))
        self.assertEqual(m.execute("SELECT favicon_path FROM sites WHERE id='s1'").fetchone()[0], favicon.site_name('s1'))

    def test_new_key_names_are_never_listed_as_old(self):
        make_legacy(self.d2.parent / 'k.sqlite', [('s9', favicon.site_name('s9'), 'ok', '2026-11-01')])   # 偶然、新キーと同じ名前が旧名にある
        items, _, old, _ = rekey.collect(self.dbs + [str(self.d2.parent / 'k.sqlite')])
        self.assertNotIn(favicon.site_name('s9'), old)

    def test_cli_guards(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rekey.main([*self.dbarg, '--store-dir', str(self.src), 'delete-old', '--d1-json', 'x', '--apply']), 2)   # --d1-done が無い
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
        self.assertIn(f"AND (favicon_path IS NOT '{favicon.site_name('s1')}' OR favicon_v IS NOT '{v2}')", favicon.sql_updates(self.db, store='r2-remote'))

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

    def test_exclude_step_then_sql_null_then_delete(self):
        favicon.record(self.db, favicon.R2Store(remote=True, runner=self.runner), self.row(), self.res((1, 2, 3, 255)), datetime_now())
        self.calls.clear()
        self.assertEqual(favicon.exclude_sites(self.db, ['s1'], apply=False, log=lambda m: None), 0)
        self.assertEqual(self.row()['exclude'], 0)
        self.assertEqual(favicon.exclude_sites(self.db, ['s1', 'nope'], apply=True, log=lambda m: None), 1)
        self.assertEqual(self.calls, [])   # R2 には触れない
        self.assertIsNotNone(self.row()['icon_hash'])   # 画像の印は残したまま(delete が消す)
        self.assertIn('favicon_path=NULL, favicon_v=NULL', favicon.sql_updates(self.db))   # D1 を先に NULL にできる
        self.assertEqual(favicon.delete_sites(self.db, favicon.R2Store(remote=True, runner=self.runner), ['s1'], apply=True, log=lambda m: None), 1)   # その後に R2

    def test_cli_exclude_dry_run_default(self):
        t = Path(self.tmp.name) / 'f.sqlite'
        db = favicon.connect(t); favicon.import_sites(db, [('s1', 'https://a.example/')]); db.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(favicon.main(['--db', str(t), 'exclude', '--site', 's1']), 0)
            self.assertEqual(sqlite3.connect(t).execute('SELECT exclude FROM favicons').fetchone()[0], 0)
            self.assertEqual(favicon.main(['--db', str(t), 'exclude', '--site', 's1', '--apply']), 0)
        self.assertEqual(sqlite3.connect(t).execute('SELECT exclude FROM favicons').fetchone()[0], 1)

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
