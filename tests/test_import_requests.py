import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'requests'))
sys.path.insert(0, str(ROOT / 'scripts' / 'recrawl'))
import import_requests as ir
import frontier
import recrawl


def row(i, typ, url, reason='理由', nk=None):
    host = url.split('//')[-1].split('/')[0]
    return {'id': i, 'type': typ, 'url': url, 'host': host, 'normalized_key': nk or ir.key_of(url), 'reason': reason, 'created_at': '2026-11-14 00:00:00'}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = frontier.connect(self.dir / 'f.sqlite')
        self.review = self.dir / 'review.jsonl'
        self.listed = ({'listed.example': 'collected-listed', 'blog.example/me': 'collected-me'}, {'listed.example': 'collected-listed', 'blog.example': 'collected-me'})

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def run_import(self, rows, blocked=()):
        return ir.import_requests(rows, self.db, self.review, blocked, self.listed)

    def reviews(self):
        return [json.loads(l) for l in self.review.read_text(encoding='utf-8').splitlines()] if self.review.exists() else []


class ImportTest(Base):
    def test_add_goes_to_frontier(self):
        d, c = self.run_import([row(1, 'add', 'https://www.new-site.example/')])
        r = self.db.execute("SELECT * FROM frontier WHERE key='new-site.example'").fetchone()
        self.assertEqual((r['source'], r['source_detail'], r['state']), ('request', 'request#1', 'discovered'))
        self.assertEqual(d[1], ('approved', 'imported')); self.assertEqual(c['frontier'], 1)
        self.assertEqual(self.reviews(), [])

    def test_add_duplicates_skipped(self):
        self.db.execute("INSERT INTO frontier (key,host,url,source,discovered_at) VALUES ('dup.example','dup.example','https://dup.example/','links','t')")
        d, c = self.run_import([row(1, 'add', 'https://dup.example/'), row(2, 'add', 'https://www.listed.example/')])
        self.assertEqual(d[1], ('approved', 'duplicate')); self.assertEqual(d[2], ('approved', 'duplicate'))
        self.assertEqual(c['duplicate'], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM frontier').fetchone()[0], 1)

    def test_add_blocklisted_rejected_and_not_added(self):
        blocked = [{'host': 'bad.example', 'path_prefix': ''}, {'host': 'svc.example', 'path_prefix': '/foo'}]
        d, _ = self.run_import([row(1, 'add', 'https://sub.bad.example/'), row(2, 'add', 'https://svc.example/foo/x'),
                                row(3, 'add', 'https://svc.example/other')], blocked)
        self.assertEqual(d[1], ('rejected', 'blocklisted')); self.assertEqual(d[2], ('rejected', 'blocklisted'))
        self.assertEqual(d[3][0], 'approved')
        self.assertEqual(self.db.execute('SELECT key FROM frontier').fetchall()[0]['key'], 'svc.example')

    def test_remove_correct_to_review_file_with_reason(self):
        d, c = self.run_import([row(1, 'remove', 'https://blog.example/me', '消したい'), row(2, 'correct', 'https://nolisted.example/', '名前が違う')])
        rv = self.reviews()
        self.assertEqual([(r['type'], r['state'], r['site_id'], r['reason']) for r in rv],
                         [('remove', 'request_remove', 'collected-me', '消したい'), ('correct', 'request_correct', None, '名前が違う')])
        self.assertEqual(d[1], ('approved', 'imported')); self.assertEqual(c['review'], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM frontier').fetchone()[0], 0)

    def test_idempotent(self):
        rows = [row(1, 'add', 'https://new.example/'), row(2, 'remove', 'https://listed.example/')]
        self.run_import(rows); d2, _ = self.run_import(rows)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM frontier').fetchone()[0], 1)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(set(d2), {1, 2})  # SQL は再度出る(適用済みなら pending でなく読まれない)

    def test_emit_sql(self):
        sql = ir.emit_sql({1: ('approved', 'imported'), 2: ('rejected', "blocklisted")})
        self.assertIn("UPDATE requests SET status='approved', note='imported', decided_at=datetime('now') WHERE id=1 AND status='pending';", sql)
        self.assertIn("status='rejected', note='blocklisted'", sql)
        self.assertNotIn('DELETE', sql); self.assertNotIn('blocked_hosts', sql)

    def test_is_blocked_www_and_path(self):
        self.assertTrue(ir.is_blocked('a.bad.example', [{'host': 'bad.example', 'path_prefix': ''}]))
        self.assertFalse(ir.is_blocked('notbad.example', [{'host': 'bad.example', 'path_prefix': ''}]))
        self.assertFalse(ir.is_blocked('s.svc.example/foo', [{'host': 'svc.example', 'path_prefix': '/foo'}]))


class D1ReadTest(unittest.TestCase):
    def fake(self, store):
        def runner(cmd, **kw):
            store.append(cmd)
            out = json.dumps([{'results': [{'id': 1}], 'success': True}])
            return subprocess.CompletedProcess(cmd, 0, 'banner\n' + out, '')
        return runner

    def test_local_is_default_and_remote_is_explicit(self):
        calls = []
        ir.d1_select('SELECT 1', runner=self.fake(calls)); ir.d1_select('SELECT 1', remote=True, runner=self.fake(calls))
        self.assertIn('--local', calls[0]); self.assertNotIn('--remote', calls[0])
        self.assertIn('--remote', calls[1]); self.assertNotIn('--persist-to', calls[1])

    def test_main_remote_only_selects_and_emits_sql(self):
        calls = []
        def runner(cmd, **kw):
            calls.append(cmd)
            sql = cmd[cmd.index('--command') + 1]
            res = [{'id': 5, 'type': 'add', 'url': 'https://m.example/', 'host': 'm.example', 'normalized_key': 'm.example', 'reason': 'r', 'created_at': 't'}] if 'FROM requests' in sql else []
            return subprocess.CompletedProcess(cmd, 0, json.dumps([{'results': res, 'success': True}]), '')
        with tempfile.TemporaryDirectory() as t:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ir.main(['--remote', '--emit-sql', '--frontier-db', f'{t}/f.sqlite', '--review-file', f'{t}/r.jsonl'], runner=runner)
        self.assertTrue(all('--remote' in c for c in calls))
        self.assertTrue(all(c[c.index('--command') + 1].lstrip().upper().startswith('SELECT') for c in calls))
        self.assertIn('WHERE id=5', buf.getvalue())

    def test_failure_raises(self):
        with self.assertRaises(RuntimeError):
            ir.d1_select('SELECT 1', runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'boom'))


class RecrawlCandidatesTest(Base):
    def test_request_candidates_read(self):
        self.run_import([row(1, 'remove', 'https://listed.example/', '消して')])
        r = recrawl.request_candidates(self.review)
        self.assertEqual(r[0]['site_id'], 'collected-listed'); self.assertEqual(recrawl.request_candidates(self.dir / 'none'), [])


if __name__ == '__main__':
    unittest.main()
