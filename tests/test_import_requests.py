import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'requests'))
sys.path.insert(0, str(ROOT / 'scripts' / 'recrawl'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
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
        self.listed = {'listed.example': 'collected-listed', 'blog.example/me': 'collected-me', 'asahi.example/~dy7s': 'collected-dy7s'}

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def run_import(self, rows, blocked=()):
        return ir.import_requests(rows, self.db, self.review, blocked, self.listed)

    def reviews_raw(self):
        return self.review.read_text(encoding='utf-8').splitlines()

    def reviews(self):
        return [json.loads(l) for l in self.review.read_text(encoding='utf-8').splitlines() if l.startswith('{"request_id"') and 'type' in l] if self.review.exists() else []


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
        self.assertEqual(self.db.execute('SELECT key FROM frontier').fetchall()[0]['key'], 'svc.example/other')

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

    def test_shared_hosting_user_not_blocked_by_parent(self):
        row = [{'host': 'github.io', 'path_prefix': ''}]
        self.assertFalse(ir.is_blocked('user.github.io', row))
        self.assertTrue(ir.is_blocked('user.github.io', [{'host': 'user.github.io', 'path_prefix': ''}]))
        self.assertTrue(ir.is_blocked('a.user.github.io', [{'host': 'user.github.io', 'path_prefix': ''}]))

    def test_key_of_matches_intake_normalization(self):
        self.assertEqual(ir.key_of('https://WWW.Example.com/%7Ebob//a;x=1/./b/'), 'example.com/~bob/a/b')
        self.assertEqual(ir.key_of('http://example.com/foo%2Fbar/'), 'example.com/foo/bar')
        self.assertEqual(ir.key_of('https://example.com/a/../b.'), 'example.com/b')

    def test_path_type_add_is_per_user(self):
        d, c = self.run_import([row(1, 'add', 'https://asahi.example/~other'), row(2, 'add', 'https://asahi.example/~dy7s'),
                                row(3, 'add', 'https://ameblo.example/bar'), row(4, 'add', 'https://ameblo.example/baz')])
        self.assertEqual(d[1], ('approved', 'imported')); self.assertEqual(d[2], ('approved', 'duplicate'))
        keys = {r['key'] for r in self.db.execute('SELECT key FROM frontier')}
        self.assertEqual(keys, {'asahi.example/~other', 'ameblo.example/bar', 'ameblo.example/baz'})

    def test_us_host_is_imported_and_not_rejected_by_triage(self):
        import run as pipeline_run
        self.run_import([row(1, 'add', 'https://us-site.com/')])
        pipeline_run.triage(self.db)
        r = self.db.execute("SELECT state, reject_reason FROM frontier WHERE key='us-site.com'").fetchone()
        self.assertEqual(r['state'], 'queued')

    def test_path_request_site_id_only_on_exact_match(self):
        self.run_import([row(1, 'remove', 'https://asahi.example/~other'), row(2, 'remove', 'https://asahi.example/~dy7s/'), row(3, 'correct', 'https://blog.example/other')])
        self.assertEqual([r['site_id'] for r in self.reviews()], [None, 'collected-dy7s', None])

    def test_rerun_without_applying_sql_keeps_imported(self):
        rows = [row(1, 'add', 'https://new.example/')]
        d1, _ = self.run_import(rows); d2, c2 = self.run_import(rows)
        self.assertEqual(d1[1], d2[1]); self.assertEqual(d2[1], ('approved', 'imported')); self.assertEqual(c2['frontier'], 1)

    def test_broken_review_line_does_not_stop(self):
        self.review.write_text('{broken\n' + json.dumps({'request_id': 1}) + '\n', encoding='utf-8')
        d, c = self.run_import([row(1, 'remove', 'https://listed.example/'), row(2, 'remove', 'https://listed.example/')])
        self.assertEqual(len(self.reviews_raw()), 3)   # 壊れた行 + 既存 1 + 新規 2
        self.assertEqual(c['review'], 2)

    def test_mark_reviewed_hides_from_candidates(self):
        self.run_import([row(1, 'remove', 'https://listed.example/'), row(2, 'correct', 'https://listed.example/')])
        ir.mark_reviewed(self.review, [1])
        self.assertEqual([q['request_id'] for q in recrawl.request_candidates(self.review)], [2])
        self.run_import([row(1, 'remove', 'https://listed.example/')])   # 再取り込みしても復活しない
        self.assertEqual(len(self.reviews()), 2)

    def test_listed_index_prefers_d1_rows(self):
        idx = ir.listed_index([{'id': 'collected-hyuki', 'url': 'https://www.hyuki.com/'}], files=[])
        self.assertEqual(idx, {'hyuki.com': 'collected-hyuki'})

    def test_shared_suffixes_match_intake_js(self):
        out = subprocess.run(['node', '--input-type=module', '-e', "import {SHARED_SUFFIXES as S} from './src/intake.js'; console.log(JSON.stringify(S))"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual(ir._shared_suffixes(), json.loads(out))

    def test_request_keys_do_not_exempt_shared_service_deferral(self):
        import run as pipeline_run
        for k, h, src in (('u1.sharedsvc.jp', 'u1.sharedsvc.jp', 'commoncrawl-webgraph'), ('u2.sharedsvc.jp', 'u2.sharedsvc.jp', 'commoncrawl-webgraph'),
                          ('u3.sharedsvc.jp', 'u3.sharedsvc.jp', 'commoncrawl-webgraph'), ('sharedsvc.jp/someone', 'sharedsvc.jp', 'request'), ('sharedsvc.jp', 'sharedsvc.jp', 'request')):
            frontier.add(self.db, k, h, f'https://{h}/', src)
        pipeline_run.triage(self.db)
        st = {r['key']: r['state'] for r in self.db.execute('SELECT key, state FROM frontier')}
        self.assertEqual({st['u1.sharedsvc.jp'], st['u2.sharedsvc.jp'], st['u3.sharedsvc.jp']}, {'deferred'})


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
            if 'FROM sites' in sql:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([{'results': [{'id': 'collected-x', 'url': 'https://x.example/'}], 'success': True}]), '')
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

    def test_mark_reviewed_warns_on_unknown_id(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            ir.main(['--review-file', f'{t}/r.jsonl', '--mark-reviewed', '99'])
        self.assertIn('申請#99', err.getvalue())

    def test_failure_raises(self):
        with self.assertRaises(RuntimeError):
            ir.d1_select('SELECT 1', runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'boom'))


class RecrawlCandidatesTest(Base):
    def test_request_candidates_read(self):
        self.run_import([row(1, 'remove', 'https://listed.example/', '消して')])
        r = recrawl.request_candidates(self.review)
        self.assertEqual(r[0]['site_id'], 'collected-listed'); self.assertEqual(recrawl.request_candidates(self.dir / 'none'), [])

    def test_safe_text_strips_escapes_newlines_and_truncates(self):
        t = recrawl.safe_text('a\x1b[31mred\x1b[0m\r\nfake line\x07\u202e' + 'x' * 50000)
        self.assertNotIn('\x1b', t); self.assertNotIn('\n', t); self.assertNotIn('\r', t); self.assertNotIn('\x07', t); self.assertNotIn('\u202e', t)
        self.assertIn('\\nfake line', t); self.assertLessEqual(len(t), 201)

    def test_candidates_cli_output_is_one_safe_line_and_json_has_kind(self):
        import io, contextlib
        self.run_import([row(1, 'remove', 'https://listed.example/', 'x\x1b[2J\nFAKE [needs_review] line' + 'y' * 50000)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            recrawl.main(['--db', str(self.dir / 'rc.sqlite'), 'candidates', '--requests-file', str(self.review)])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1); self.assertNotIn('\x1b', lines[0]); self.assertLess(len(lines[0]), 400)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            recrawl.main(['--db', str(self.dir / 'rc.sqlite'), 'candidates', '--json', '--requests-file', str(self.review)])
        self.assertEqual([e['kind'] for e in json.loads(out.getvalue())], ['request'])


if __name__ == '__main__':
    unittest.main()
