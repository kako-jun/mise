import datetime
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / 'recrawl'))
import recrawl
from recrawl import fetcher

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 14, tzinfo=UTC)


class Handler(BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.log.append((self.path, self.headers.get('User-Agent')))
        p = self.path
        if p == '/robots.txt':
            return self.send(200, 'User-agent: MiseResearchBot\nDisallow: /blocked\n')
        if p == '/ok':
            return self.send(200, '<html><head><title>Hello</title></head><body>v1 text <script>x=1</script></body></html>')
        if p == '/ok2':
            return self.send(200, '<html><head><title>Hello</title></head><body>v2 changed</body></html>')
        if p == '/retitle':
            return self.send(200, '<html><head><title>New</title></head><body>v1 text</body></html>')
        if p == '/five':
            return self.send(503, 'down')
        if p == '/gone':
            return self.send(404, 'nf')
        if p == '/denied':
            return self.send(403, 'no')
        if p == '/redir':
            return self.send(302, '', {'Location': '/ok'})
        if p == '/off':
            return self.send(302, '', {'Location': 'http://other-domain.example/'})
        if p == '/slow':
            time.sleep(3)
            return self.send(200, 'late')
        if p == '/blocked':
            return self.send(200, 'should not be fetched')
        self.send(404, 'nf')

    def send(self, code, body, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode())


class Local(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.saved = (fetcher.ALLOWED_PORTS, fetcher.ALLOW_PRIVATE, fetcher.TIMEOUT, dict(os.environ))
        fetcher.ALLOWED_PORTS = (cls.port,); fetcher.ALLOW_PRIVATE = True; fetcher.TIMEOUT = 1
        for k in [k for k in os.environ if k.lower().endswith('_proxy')]:
            del os.environ[k]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        fetcher.ALLOWED_PORTS, fetcher.ALLOW_PRIVATE, fetcher.TIMEOUT, env = cls.saved
        os.environ.clear(); os.environ.update(env)

    def check(self, path):
        robots = fetcher.RobotsCache(lambda h: None)
        return recrawl.check_url(f'http://127.0.0.1:{self.port}{path}', robots, sleep=lambda s: None)

    def test_classification(self):
        self.assertEqual(self.check('/ok')['kind'], 'ok')
        self.assertEqual(self.check('/ok')['title'], 'Hello')
        self.assertEqual(self.check('/five')['kind'], 'http_5xx')
        self.assertEqual(self.check('/gone')['kind'], 'http_404')
        self.assertEqual(self.check('/denied')['kind'], 'http_denied')
        r = self.check('/redir')
        self.assertEqual((r['kind'], r['final_url'].endswith('/ok')), ('ok', True))
        self.assertEqual(self.check('/off')['kind'], 'redirect_offsite')

    def test_timeout(self):
        self.assertEqual(self.check('/slow')['kind'], 'timeout')

    def test_robots_disallowed_not_fetched(self):
        Handler.log.clear()
        self.assertEqual(self.check('/blocked')['kind'], 'robots_disallowed')
        self.assertNotIn('/blocked', [p for p, _ in Handler.log])

    def test_user_agent(self):
        Handler.log.clear(); self.check('/ok')
        self.assertTrue(all(ua.startswith('MiseResearchBot') for _, ua in Handler.log))

    def test_dns_failure(self):
        fetcher.ALLOW_PRIVATE = False
        try:
            robots = fetcher.RobotsCache(lambda h: None)
            r = recrawl.check_url(f'http://nonexistent.invalid:{self.port}/', robots, sleep=lambda s: None)
        finally:
            fetcher.ALLOW_PRIVATE = True
        self.assertEqual(r['kind'], 'dns')

    def test_run_end_to_end(self):
        db = recrawl.connect(':memory:')
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'sites.json'
            f.write_text(json.dumps([{'id': 'a', 'url': f'http://127.0.0.1:{self.port}/ok', 'name': 'A'},
                                     {'id': 'b', 'url': f'http://127.0.0.1:{self.port}/five', 'name': 'B'}]))
            self.assertEqual(recrawl.import_sites(db, [f]), 2)
        n = recrawl.run(db, workers=2, host_wait=0, now=T0, log=lambda *_: None)
        self.assertEqual(n, 2)
        rows = {r['site_id']: r for r in db.execute('SELECT * FROM sites')}
        self.assertEqual((rows['a']['last_kind'], rows['a']['consecutive_failures']), ('ok', 0))
        self.assertEqual((rows['b']['last_kind'], rows['b']['consecutive_failures']), ('http_5xx', 1))
        # 直後は期限前なので何も確認しない
        self.assertEqual(recrawl.run(db, host_wait=0, now=T0, log=lambda *_: None), 0)
        self.assertIn('INSERT OR REPLACE INTO site_checks', recrawl.to_sql(db))


class StateMachine(unittest.TestCase):
    def site(self, db, sid='s'):
        db.execute("INSERT OR IGNORE INTO sites(site_id,url) VALUES(?,?)", (sid, 'http://x.example/'))
        return db.execute('SELECT * FROM sites WHERE site_id=?', (sid,)).fetchone()

    def res(self, kind, title='T', h='h1'):
        return {'kind': kind, 'http_status': None, 'final_url': 'u', 'error': None, 'title': title, 'hash': h}

    def feed(self, db, kinds, start=T0, step_days=1):
        now = start
        for k in kinds:
            recrawl.apply_result(db, self.site(db), self.res(k), now)
            now += datetime.timedelta(days=step_days)
        return db.execute("SELECT * FROM sites WHERE site_id='s'").fetchone()

    def test_transitions_never_delete(self):
        db = recrawl.connect(':memory:')
        r = self.feed(db, ['dns', 'dns'])
        self.assertEqual((r['state'], r['consecutive_failures']), ('listed', 2))
        r = self.feed(db, ['dns'], start=T0 + datetime.timedelta(days=2))
        self.assertEqual(r['state'], 'needs_review')
        # 6回失敗でも14日経っていなければ 要確認のまま
        r = self.feed(db, ['dns'] * 3, start=T0 + datetime.timedelta(days=3))
        self.assertEqual((r['state'], r['consecutive_failures']), ('needs_review', 6))
        r = self.feed(db, ['dns'], start=T0 + datetime.timedelta(days=15))
        self.assertEqual(r['state'], 'hide_candidate')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sites').fetchone()[0], 1)
        self.assertEqual([c['site_id'] for c in recrawl.candidates(db)], ['s'])
        # 復旧すると listed に戻り、注記が残る
        r = self.feed(db, ['ok'], start=T0 + datetime.timedelta(days=16))
        self.assertEqual((r['state'], r['consecutive_failures'], r['first_failure_at']), ('listed', 0, None))
        self.assertIn('復旧', r['note'])

    def test_neutral_does_not_count(self):
        db = recrawl.connect(':memory:')
        r = self.feed(db, ['robots_disallowed', 'http_denied', 'robots_unavailable'] * 3)
        self.assertEqual((r['state'], r['consecutive_failures']), ('listed', 0))

    def test_offsite_redirect_needs_review(self):
        db = recrawl.connect(':memory:')
        self.assertEqual(self.feed(db, ['redirect_offsite'])['state'], 'needs_review')

    def test_change_detection_records_only(self):
        db = recrawl.connect(':memory:')
        s = self.site(db)
        recrawl.apply_result(db, s, self.res('ok', 'Old', 'h1'), T0)
        recrawl.apply_result(db, self.site(db), self.res('ok', 'New', 'h2'), T0 + datetime.timedelta(days=30))
        ch = [(r['field'], r['old'], r['new']) for r in db.execute('SELECT * FROM changes ORDER BY id')]
        self.assertEqual(ch, [('title', 'Old', 'New'), ('content_hash', 'h1', 'h2')])
        self.assertEqual(self.site(db)['title'], 'New')   # 台帳の観測値は更新、紹介文には触れない

    def test_priority_failing_then_stalest(self):
        db = recrawl.connect(':memory:')
        for sid, fails, last in [('fresh', 0, '2026-11-01T00:00:00Z'), ('old', 0, '2026-01-01T00:00:00Z'), ('failing', 2, '2026-11-10T00:00:00Z'), ('never', 0, None)]:
            db.execute("INSERT INTO sites(site_id,url,consecutive_failures,last_checked_at,next_check_at) VALUES(?,?,?,?,?)",
                       (sid, 'http://x/', fails, last, '2026-11-02T00:00:00Z' if last else None))
        order = [r['site_id'] for r in recrawl.due_sites(db, T0)]
        self.assertEqual(order, ['failing', 'never', 'old', 'fresh'])

    def test_migration_sql_applies(self):
        import sqlite3
        db = recrawl.connect(':memory:')
        self.site(db)
        recrawl.apply_result(db, self.site(db), self.res('ok'), T0)
        m = sqlite3.connect(':memory:')
        m.executescript((Path(__file__).parents[1] / 'migrations' / '0017_recrawl.sql').read_text())
        m.executescript(recrawl.to_sql(db))
        self.assertEqual(m.execute('SELECT state FROM site_checks').fetchone()[0], 'listed')


if __name__ == '__main__':
    unittest.main()
