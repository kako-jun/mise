"""取得段(scripts/enrich/fetch_stage.py)。D1 は実マイグレーションから作った in-memory SQLite、取得は偽関数。ネットワーク・本番には出ない。"""
import collections
import datetime
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
from enrich import fetch_stage as fs

PROGRESS_DDL = re.search(r'CREATE TABLE IF NOT EXISTS url_progress \(.*?\n\);', (ROOT / 'migrations' / '0032_url_progress.sql').read_text(encoding='utf-8'), re.S).group(0)
CAND_DDL = (ROOT / 'migrations' / '0036_url_candidates.sql').read_text(encoding='utf-8')
T0 = datetime.datetime(2026, 11, 24, 12, 0, 0)


class FakeD1:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute(PROGRESS_DDL)
        self.db.executescript(CAND_DDL)
        self.max_params = 0
        self.writes = 0
        self.max_batch = 0

    def query(self, sql, params=()):
        params = list(params)
        self.max_params = max(self.max_params, len(params))
        if len(params) > 100:
            raise RuntimeError('too many SQL variables')
        if not sql.lstrip().upper().startswith('SELECT'):
            self.writes += 1
        rows = [dict(r) for r in self.db.execute(sql, params).fetchall()]
        self.db.commit()
        return rows

    def batch(self, stmts):
        self.max_batch = max(self.max_batch, len(stmts))
        return [self.query(s, p) for s, p in stmts]

    def add(self, url, state='discovered', updated='2026-11-24 00:00:00'):
        from progress_identity import progress_identity_key
        self.db.execute('INSERT INTO url_progress(identity_key,url,state,updated_at) VALUES(?,?,?,?)', (progress_identity_key(url), url, state, updated))
        self.db.commit()
        return progress_identity_key(url)

    def state(self, k):
        return self.db.execute('SELECT state FROM url_progress WHERE identity_key=?', (k,)).fetchone()[0]

    def cand(self, k):
        r = self.db.execute('SELECT * FROM url_candidates WHERE identity_key=?', (k,)).fetchone()
        return dict(r) if r else None


GOOD = {'title': 'たろうの部屋', 'description': '個人の日記', 'lang': 'ja', 'kana_count': 300, 'text_len': 900, 'ad_hits': 0, 'ext_refs': 0,
        'flags': '{"noindex": false, "parking": null, "adult_rating": false, "adult_text": null, "adult_weak": 0}',
        'text_head': 'あ' * 500, 'personal_score': 3, 'freshness_signal_at': '2026-11-01', 'freshness_source': 'footer'}


def ok(**kw):
    return {'error': None, 'status': 200, 'robots_status': 'allowed', 'final_url': 'https://x/', 'feat': {**GOOD, **kw}}


def err(code, detail='', status=None, dns_own=None):
    return {'error': code, 'error_detail': detail, 'status': status, 'robots_status': None, 'final_url': None, 'feat': None, 'dns_own': dns_own}


def fake(table):
    def f(url):
        v = table[url]
        if isinstance(v, Exception):
            raise v
        return v
    return f


def go(d, table, limit=100, apply=True, now=T0):
    return fs.run(d, limit=limit, apply=apply, log=lambda *_: None, fetch=fake(table), now=lambda: now, workers=2)


class FetchStage(unittest.TestCase):
    def test_fetched_writes_candidate(self):
        d = FakeD1(); k = d.add('https://taro-home.jp/')
        s = go(d, {'https://taro-home.jp/': ok()})
        self.assertEqual(s['fetched'], 1); self.assertEqual(s.get('verify_failed', 0), 0)
        self.assertEqual(d.state(k), 'fetched')
        c = d.cand(k)
        self.assertEqual((c['title'], c['state'], c['fetch_attempts'], c['last_http_status'], c['frontier_key'], c['host']), ('たろうの部屋', 'fetched', 1, 200, 'taro-home.jp', 'taro-home.jp'))
        self.assertEqual(len(c['text_head']), fs.TEXT_HEAD_MAX); self.assertEqual(c['fetch_adult'], 'unknown')
        self.assertIsNone(c['spam_verdict'])   # 判定側の列には触れない

    def test_personal_name_domain_proceeds(self):
        d = FakeD1(); d.add('https://yamada-taro.jp/')
        self.assertEqual(go(d, {'https://yamada-taro.jp/': ok()})['fetched'], 1)

    def test_terminal_states(self):
        d = FakeD1()
        cases = {'https://a-robots.jp/': (err('robots_disallowed'), 'blocked', 'robots'),
                 'https://b-nx.jp/': (err('dns', 'x', dns_own='[Errno -2] Name or service not known'), 'failed', 'dns_nxdomain'),
                 'https://c-404.jp/': ({**ok(), 'status': 404, 'feat': None}, 'failed', 'http_404'),
                 'https://d-410.jp/': ({**ok(), 'status': 410, 'feat': None}, 'failed', 'http_410'),
                 'https://e-pdf.jp/': (err('not_html'), 'failed', 'not_html'),
                 'https://f-off.jp/': (err('redirect_offsite'), 'rejected', 'redirect_offsite'),
                 'https://g-park.jp/': (ok(flags='{"parking": "domain for sale", "noindex": false}'), 'rejected', 'parking_or_default_page'),
                 'https://h-noidx.jp/': (ok(flags='{"noindex": true}'), 'rejected', 'noindex')}
        cases['https://i-403.jp/'] = ({**ok(), 'status': 403, 'feat': None}, 'blocked', 'http_403')
        keys = {u: d.add(u) for u in cases}
        go(d, {u: v[0] for u, v in cases.items()})
        for u, (_, st, reason) in cases.items():
            self.assertEqual(d.state(keys[u]), st, u)
            self.assertEqual(d.cand(keys[u])['reject_reason'], reason, u)

    def test_adult_flagged_not_rejected(self):
        d = FakeD1(); k = d.add('https://adult-ex.jp/')
        go(d, {'https://adult-ex.jp/': ok(flags='{"adult_rating": true, "noindex": false}')})
        self.assertEqual(d.state(k), 'fetched')
        c = d.cand(k); self.assertEqual(c['fetch_adult'], 'yes'); self.assertIsNone(c['text_head']); self.assertIsNone(c['description'])

    def test_non_site_rejected_before_fetch(self):
        d = FakeD1(); k = d.add('https://www.city.kanazawa.ishikawa.jp/')
        called = []
        s = fs.run(d, limit=10, apply=True, log=lambda *_: None, fetch=lambda u: called.append(u), now=lambda: T0)
        self.assertEqual(d.state(k), 'rejected'); self.assertEqual(called, []); self.assertEqual(d.cand(k)['reject_reason'], fs.PUBLIC_BODY_REASON)

    def test_transient_backoff_and_cap(self):
        d = FakeD1(); u = 'https://slow-site.jp/'; k = d.add(u)
        now = T0
        for n in range(1, 5):
            s = go(d, {u: err('connect', 'timeout')}, now=now)
            self.assertEqual(s['retry'], 1); self.assertEqual(d.state(k), 'discovered')
            c = d.cand(k); self.assertEqual(c['fetch_attempts'], n)
            due = datetime.datetime.strptime(c['next_fetch_at'], fs.FMT)
            self.assertEqual(due, now + datetime.timedelta(hours=fs.BACKOFF_HOURS * 2 ** (n - 1)))
            # 待ち時間内は選ばれない
            self.assertEqual(go(d, {u: err('connect')}, now=now + datetime.timedelta(minutes=1))['selected'], 0)
            now = due
        s = go(d, {u: err('connect', 'timeout')}, now=now)
        self.assertEqual(s['failed'], 1); self.assertEqual(d.state(k), 'failed'); self.assertEqual(d.cand(k)['fetch_attempts'], 5)

    def test_transient_statuses(self):
        d = FakeD1(); ks = {}
        table = {}
        for i, st in enumerate((429, 500, 503)):
            u = f'https://t{i}-site.jp/'; ks[u] = d.add(u); table[u] = {**ok(), 'status': st, 'feat': None}
        u = 'https://dnsfail-site.jp/'; ks[u] = d.add(u); table[u] = err('dns', '[Errno -3] Temporary failure in name resolution')
        go(d, table)
        for k in ks.values():
            self.assertEqual(d.state(k), 'discovered')

    def test_exception_is_transient(self):
        d = FakeD1(); k = d.add('https://boom-site.jp/')
        go(d, {'https://boom-site.jp/': RuntimeError('x')})
        self.assertEqual(d.state(k), 'discovered'); self.assertEqual(d.cand(k)['last_error'], 'exception:RuntimeError')

    def test_claim_exclusive_and_stale(self):
        d = FakeD1()
        keys = [d.add(f'https://site{i:02d}-ex.jp/') for i in range(5)]
        rows_a = fs.select_rows(d, 10, T0); rows_b = fs.select_rows(d, 10, T0)
        a = fs.claim(d, rows_a, T0); b = fs.claim(d, rows_b, T0)
        self.assertEqual(len(a), 5); self.assertEqual(b, [])
        for k in keys: self.assertEqual(d.state(k), 'queued')
        self.assertEqual(fs.select_rows(d, 10, T0 + datetime.timedelta(minutes=10)), [])
        stale = fs.select_rows(d, 10, T0 + datetime.timedelta(minutes=31))
        self.assertEqual(len(stale), 5)
        self.assertEqual(len(fs.claim(d, stale, T0 + datetime.timedelta(minutes=31))), 5)

    def test_order_and_limit(self):
        d = FakeD1()
        for n in ('zz', 'aa', 'mm'): d.add(f'https://{n}-order.jp/')
        rows = fs.select_rows(d, 2, T0)
        self.assertEqual([r['identity_key'] for r in rows], ['aa-order.jp/', 'mm-order.jp/'])

    def test_param_limit_with_many_rows(self):
        d = FakeD1()
        table = {}
        for i in range(130):
            u = f'https://bulk{i:03d}-ex.jp/'; d.add(u); table[u] = ok()
        s = go(d, table, limit=500)
        self.assertEqual(s['fetched'], 130); self.assertLessEqual(d.max_params, 100); self.assertLessEqual(d.max_batch, fs.BATCH_STATEMENTS)

    def test_dry_run_writes_nothing(self):
        d = FakeD1(); k = d.add('https://dry-site.jp/')
        called = []
        s = fs.run(d, limit=10, apply=False, log=lambda *_: None, fetch=lambda u: called.append(u), now=lambda: T0)
        self.assertEqual((s['selected'], d.writes, called, d.state(k), d.cand(k)), (1, 0, [], 'discovered', None))

    def test_no_files_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd(); os.chdir(tmp)
            try:
                d = FakeD1(); d.add('https://nofile-site.jp/'); go(d, {'https://nofile-site.jp/': ok()})
                self.assertEqual(os.listdir(tmp), [])
            finally:
                os.chdir(cwd)

    def test_rerun_keeps_judgement_columns(self):
        d = FakeD1(); u = 'https://keep-judge.jp/'; k = d.add(u)
        go(d, {u: ok()})
        d.db.execute("UPDATE url_candidates SET spam_verdict='ok', categories='x' WHERE identity_key=?", (k,)); d.db.execute("UPDATE url_progress SET state='discovered' WHERE identity_key=?", (k,)); d.db.commit()
        go(d, {u: ok(title='新題')})
        c = d.cand(k); self.assertEqual((c['spam_verdict'], c['categories'], c['title'], c['fetch_attempts']), ('ok', 'x', '新題', 2))

    def test_shared_service_and_isp_sites_are_fetched(self):
        d = FakeD1()
        urls = ['https://user.hatenablog.com/', 'https://someone.github.io/', 'https://isp-example.ne.jp/~user/', 'https://0762231234.jp/',
                'https://abcdefghijklmnopqrstuvwxyz-0123456789.jp/']
        keys = [d.add(u) for u in urls]
        s = go(d, {u: ok() for u in urls})
        self.assertEqual(s['fetched'], len(urls))
        for k in keys: self.assertEqual(d.state(k), 'fetched')

    def test_platform_page_rejected_without_fetch(self):
        d = FakeD1(); called = []
        from hostnames import blocklist
        host = blocklist('platform_pages.txt')['host'][0]
        k = d.add(f'https://{host}/some/user/page')
        fs.run(d, limit=5, apply=True, log=lambda *_: None, fetch=lambda u: called.append(u), now=lambda: T0)
        self.assertEqual((d.state(k), called, d.cand(k)['reject_reason']), ('rejected', [], 'platform_page'))
        self.assertIsNone(d.cand(k)['last_fetched_at'])

    def test_dns_failure_of_other_host_is_transient(self):
        d = FakeD1(); k = d.add('https://apex-ok.jp/')
        go(d, {'https://apex-ok.jp/': err('dns', '[Errno -2] Name or service not known', dns_own=None)})
        self.assertEqual(d.state(k), 'discovered')

    def test_classify_exception_does_not_strand_rows(self):
        d = FakeD1(); ka = d.add('https://aa-bad.jp/'); kb = d.add('https://bb-good.jp/')
        bad = ok(); bad['feat'] = {'flags': '{not json'}   # classify 内で例外
        s = go(d, {'https://aa-bad.jp/': bad, 'https://bb-good.jp/': ok()})
        self.assertEqual((d.state(ka), d.state(kb)), ('discovered', 'fetched'))
        self.assertEqual(s['claimed'], 2)

    def test_transient_and_pre_reject_keep_existing_features(self):
        d = FakeD1(); u = 'https://keep-feat.jp/'; k = d.add(u)
        go(d, {u: ok()})
        d.db.execute("UPDATE url_progress SET state='discovered' WHERE identity_key=?", (k,)); d.db.commit()
        go(d, {u: err('connect', 'timeout')})
        c = d.cand(k); self.assertEqual((c['title'], c['flags'] is not None, c['state'], c['fetch_attempts']), ('たろうの部屋', True, 'discovered', 2))
        d2 = FakeD1(); k2 = d2.add('https://www.city.kanazawa.ishikawa.jp/')
        d2.db.execute("INSERT INTO url_candidates(identity_key,title,last_fetched_at) VALUES(?,?,?)", (k2, 'old', 'old-at')); d2.db.commit()
        go(d2, {})
        c2 = d2.cand(k2); self.assertEqual((c2['title'], c2['last_fetched_at'], c2['state']), ('old', 'old-at', 'rejected'))

    def test_lost_claim_writes_nothing(self):
        d = FakeD1(); u = 'https://lost-claim.jp/'; k = d.add(u)
        row = fs.select_rows(d, 5, T0)[0]
        mine = fs.claim(d, [row], T0)
        # 別の run が stale として取り直した状態を作る
        d.db.execute("UPDATE url_progress SET updated_at='2026-11-24 13:00:00' WHERE identity_key=?", (k,)); d.db.commit()
        stats = collections.Counter()
        fs.write_results(d, fs.process_chunk(mine, fake({u: ok()}), 1), T0, stats, lambda *_: None)
        self.assertIsNone(d.cand(k)); self.assertEqual(d.state(k), 'queued')


if __name__ == '__main__':
    unittest.main()
