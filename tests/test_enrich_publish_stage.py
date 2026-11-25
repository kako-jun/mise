"""Stage C publish (scripts/enrich/publish_stage.py). D1 is an in-memory SQLite built from the repo's REAL migrations (schema, triggers, CHECKs).
No network, no production D1, no files."""
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
import publish_stage as ps
import integrity
import export_sites

LEAF = 'libraries'          # a leaf-ish id of data/categories.csv (parent: reference)
PARENT = 'reference'


_BASE = None


class FakeD1:
    """media_online.Cloudflare.query/.batch on SQLite. batch is ONE transaction (rolled back on any error, like D1). >100 params fails like D1."""
    def __init__(self):
        global _BASE
        mem = _BASE = _BASE or export_sites.load_db(str(ROOT))
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        mem.backup(self.db)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.row_factory = sqlite3.Row
        self.before_batch = None
        self.max_params = 0
        self.writes = 0

    def _exec(self, sql, params):
        params = list(params)
        self.max_params = max(self.max_params, len(params))
        if len(params) > 100:
            raise RuntimeError('too many SQL variables')
        try:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]
        except sqlite3.Error as e:
            raise RuntimeError(str(e))

    def query(self, sql, params=()):
        if not sql.lstrip().upper().startswith('SELECT'):
            self.writes += 1
        return self._exec(sql, params)

    def batch(self, statements):
        self.writes += 1
        if self.before_batch:
            hook, self.before_batch = self.before_batch, None
            hook(self.db)
        self.db.execute('BEGIN')
        try:
            out = [self._exec(s, p) for s, p in statements]
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        self.db.execute('COMMIT')
        return out

    def one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()


def add_candidate(d1, key='alpha.example.jp', state='approved', **kw):
    row = dict(identity_key=key + '/', frontier_key=key, host=key, url='https://' + key + '/', final_url='https://' + key + '/', title='Alpha Page',
               description_ja='アルファの個人サイト。', description_en='Personal site of Alpha.', categories=json.dumps([LEAF]), tags=json.dumps(['猫|cat', '犬|dog']),
               adult='no', country_code='JP', region_code='JP-13', spam_verdict='pass', last_fetched_at='2026-11-24T11:26:33+00:00',
               review_state='approved')
    row.update(kw)
    cols = ','.join(row)
    d1.db.execute(f'INSERT INTO url_candidates ({cols}) VALUES ({",".join("?" * len(row))})', list(row.values()))
    d1.db.execute("INSERT INTO url_progress(identity_key,url,state) VALUES(?,?,?) ON CONFLICT(identity_key) DO UPDATE SET state=excluded.state", (key + '/', row['url'] or 'https://x/', state))


class Publish(unittest.TestCase):
    def setUp(self):
        self.d1 = FakeD1()
        self.logs = []
        self.base_sites = self.d1.one('SELECT COUNT(*) FROM sites')[0]

    def run_stage(self, apply=True, limit=50):
        return ps.run(self.d1, limit=limit, apply=apply, log=self.logs.append)

    def test_normal_publish(self):
        add_candidate(self.d1)
        before = self.d1.one('SELECT n FROM scope_counts WHERE scope=?', ('all',))[0]
        res = self.run_stage()
        self.assertEqual((res['published'], res['held'], res['errors']), (1, 0, 0))
        sid = ps.site_id('alpha.example.jp')
        s = self.d1.one('SELECT * FROM sites WHERE id=?', (sid,))
        self.assertEqual((s['url'], s['name'], s['description_en'], s['country_code'], s['region_code'], s['adult_content'], s['bulk_import']),
                         ('https://alpha.example.jp/', 'Alpha Page', 'Personal site of Alpha.', 'JP', 'JP-13', 0, 0))
        self.assertEqual(s['progress_key'], 'alpha.example.jp/')
        self.assertEqual(s['last_crawled_at'], '2026-11-24T11:26:33Z')
        self.assertTrue(s['registered_at'])                       # trigger 0006
        self.assertIn('alpha', s['search_text'])                  # trigger: search text incl. tags
        self.assertIn('cat', s['search_text'])
        self.assertEqual([r[0] for r in self.d1.db.execute('SELECT category_id FROM site_categories WHERE site_id=?', (sid,))], [LEAF])
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM site_tags WHERE site_id=?', (sid,))[0], 2)
        jobs = {r['kind']: r for r in self.d1.db.execute('SELECT * FROM site_media_jobs WHERE site_id=?', (sid,))}
        self.assertEqual({k: (v['state'], v['eligible'], v['identity_key']) for k, v in jobs.items()},
                         {'favicon': ('pending', 1, 'alpha.example.jp/'), 'thumbnail': ('pending', 1, 'alpha.example.jp/')})
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='alpha.example.jp/'")[0], 'published')
        self.assertEqual(self.d1.one('SELECT n FROM scope_counts WHERE scope=?', ('all',))[0], before + 1)
        self.assertEqual(res['integrity'], {})
        self.assertEqual(integrity.errors_of(integrity.run_checks(lambda q: self.d1.db.execute(q).fetchall())) and
                         {n: r for n, r in integrity.errors_of(integrity.run_checks(lambda q: self.d1.db.execute(q).fetchall())).items() if r}, {})

    def test_site_id_convention(self):
        self.assertRegex(ps.site_id('alpha.example.jp'), r'^fr-alpha-example-jp-[0-9a-f]{8}$')

    def test_already_existing_site_untouched(self):
        add_candidate(self.d1)
        self.d1.db.execute("INSERT INTO sites(id,url,name,description_ja,description_en) VALUES('manual-1','http://www.Alpha.example.jp','Hand name','手書き','Hand')")
        self.d1.db.execute("INSERT INTO site_categories VALUES('manual-1','arts')")
        self.d1.db.execute("UPDATE url_progress SET state='approved' WHERE identity_key='alpha.example.jp/'")
        res = self.run_stage()
        self.assertEqual((res['already_listed'], res['published']), (1, 0))
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites + 1)
        self.assertIsNone(self.d1.one('SELECT 1 FROM sites WHERE id=?', (ps.site_id('alpha.example.jp'),)))
        s = self.d1.one("SELECT name,description_ja FROM sites WHERE id='manual-1'")
        self.assertEqual((s[0], s[1]), ('Hand name', '手書き'))
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='alpha.example.jp/'")[0], 'published')

    def test_gate_failures_go_to_candidate_with_reason(self):
        add_candidate(self.d1, 'nocat.example.jp', categories=json.dumps(['no-such-cat']))
        add_candidate(self.d1, 'nodesc.example.jp', description_en='')
        add_candidate(self.d1, 'badverdict.example.jp', spam_verdict='reject')
        add_candidate(self.d1, 'badjson.example.jp', tags='{oops')
        add_candidate(self.d1, 'nourl.example.jp', url=None, final_url=None)
        res = self.run_stage()
        self.assertEqual((res['held'], res['published']), (5, 0))
        for k, reason in [('nocat.example.jp', 'gate:no_valid_category'), ('nodesc.example.jp', 'gate:no_description'), ('badverdict.example.jp', 'gate:verdict_reject'),
                          ('badjson.example.jp', 'gate:bad_tags_json'), ('nourl.example.jp', 'gate:no_url')]:
            self.assertEqual(self.d1.one('SELECT state FROM url_progress WHERE identity_key=?', (k + '/',))[0], 'candidate')
            self.assertEqual(self.d1.one('SELECT review_reason FROM url_candidates WHERE identity_key=?', (k + '/',))[0], reason)
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites)

    def test_blocked_host_is_held_not_published(self):
        add_candidate(self.d1)
        self.d1.db.execute("INSERT INTO blocked_hosts(host,path_prefix,reason) VALUES('alpha.example.jp','','test')")
        res = self.run_stage()
        self.assertEqual((res['held'], res['published']), (1, 0))
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='alpha.example.jp/'")[0], 'candidate')
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites)

    def test_partial_failure_is_atomic(self):
        add_candidate(self.d1)
        add_candidate(self.d1, 'beta.example.jp', tags=json.dumps(['爆|boom']))
        # a failure at the tags step of ONE site: the whole batch of that site is rolled back (site row, categories, media jobs, counters)
        self.d1.db.execute("CREATE TRIGGER poison BEFORE INSERT ON site_tags WHEN NEW.label_ja='爆' BEGIN SELECT RAISE(ABORT,'poison'); END")
        base = self.d1.one('SELECT n FROM scope_counts WHERE scope=?', ('all',))[0]
        res = self.run_stage()
        self.assertEqual((res['published'], res['errors']), (1, 1))
        sid = ps.site_id('beta.example.jp')
        for t in ('sites', 'site_categories', 'site_tags', 'site_media_jobs'):
            col = 'id' if t == 'sites' else 'site_id'
            self.assertEqual(self.d1.one(f'SELECT COUNT(*) FROM {t} WHERE {col}=?', (sid,))[0], 0, t)
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='beta.example.jp/'")[0], 'approved')   # retried next run
        self.assertEqual(self.d1.one('SELECT n FROM scope_counts WHERE scope=?', ('all',))[0], base + 1)

    def test_adult_site_thumbnail_excluded(self):
        add_candidate(self.d1, adult='yes', title='Racy Title')
        res = self.run_stage()
        self.assertEqual(res['published'], 1)
        sid = ps.site_id('alpha.example.jp')
        s = self.d1.one('SELECT name,adult_content FROM sites WHERE id=?', (sid,))
        self.assertEqual((s[0], s[1]), ('alpha.example.jp', 1))   # adult=yes: the host, not the title
        t = self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id=? AND kind='thumbnail'", (sid,))
        f = self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id=? AND kind='favicon'", (sid,))
        self.assertEqual((tuple(t), tuple(f)), (('excluded', 0), ('pending', 1)))

    def test_adult_unknown_is_null_and_eligible(self):
        add_candidate(self.d1, adult='unknown')
        self.run_stage()
        sid = ps.site_id('alpha.example.jp')
        self.assertIsNone(self.d1.one('SELECT adult_content FROM sites WHERE id=?', (sid,))[0])
        self.assertEqual(tuple(self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id=? AND kind='thumbnail'", (sid,))), ('pending', 1))

    def test_ancestor_category_dropped(self):
        add_candidate(self.d1, categories=json.dumps([PARENT, LEAF]))
        self.run_stage()
        sid = ps.site_id('alpha.example.jp')
        self.assertEqual([r[0] for r in self.d1.db.execute('SELECT category_id FROM site_categories WHERE site_id=?', (sid,))], [LEAF])

    def test_param_limit(self):
        add_candidate(self.d1, tags=json.dumps(['タグ%d|tag%d' % (i, i) for i in range(30)]),
                      final_url='https://alpha.example.jp/' + 'a/' * 5, categories=json.dumps([LEAF, 'dictionaries', 'archives', 'arts']))
        res = self.run_stage()
        self.assertEqual((res['published'], res['errors']), (1, 0))
        self.assertLessEqual(self.d1.max_params, 100)
        sid = ps.site_id('alpha.example.jp')
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM site_tags WHERE site_id=?', (sid,))[0], 5)     # max 5 tags
        self.assertLessEqual(self.d1.one('SELECT COUNT(*) FROM site_categories WHERE site_id=?', (sid,))[0], 3)

    def test_dry_run_writes_nothing(self):
        add_candidate(self.d1)
        add_candidate(self.d1, 'bad.example.jp', categories='[]')
        before = self.d1.db.total_changes
        res = self.run_stage(apply=False)
        self.assertEqual((res['would_publish'], res['held'], res['published'], res['dry_run']), (1, 1, 0, True))
        self.assertEqual(self.d1.db.total_changes, before)
        self.assertEqual(self.d1.writes, 0)
        self.assertEqual(self.d1.one("SELECT state FROM url_progress WHERE identity_key='bad.example.jp/'")[0], 'approved')

    def test_limit_and_only_approved(self):
        for k in ('a.example.jp', 'b.example.jp', 'c.example.jp'):
            add_candidate(self.d1, k)
        add_candidate(self.d1, 'd.example.jp', state='fetched')
        res = self.run_stage(limit=2)
        self.assertEqual(res['published'], 2)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='approved'")[0], 1)

    def test_no_local_files(self):
        import tempfile
        add_candidate(self.d1)
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                self.run_stage()
                self.assertEqual(os.listdir(td), [])
            finally:
                os.chdir(cwd)
        src = (ROOT / 'scripts' / 'enrich' / 'publish_stage.py').read_text(encoding='utf-8')
        self.assertNotRegex(src, r'\bopen\(|sqlite3\.connect|subprocess|tempfile')

    def test_rejected_request_and_path_prefix_block(self):
        add_candidate(self.d1)
        add_candidate(self.d1, 'beta.example.jp', url='https://beta.example.jp/sub/', final_url='https://beta.example.jp/sub/')
        self.d1.db.execute("INSERT INTO requests(type,status,url,host,normalized_key) VALUES('add','rejected','https://alpha.example.jp/','alpha.example.jp','alpha.example.jp')")
        self.d1.db.execute("INSERT INTO blocked_hosts(host,path_prefix,reason) VALUES('beta.example.jp','/sub','test')")
        res = self.run_stage()
        self.assertEqual((res['published'], res['held']), (0, 2))
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites)

    def test_dry_run_matches_guards(self):
        add_candidate(self.d1)
        self.d1.db.execute("INSERT INTO blocked_hosts(host,path_prefix,reason) VALUES('alpha.example.jp','','test')")
        res = self.run_stage(apply=False)
        self.assertEqual((res['would_publish'], res['held']), (0, 1))

    def test_race_same_url_listed_between_check_and_batch(self):
        add_candidate(self.d1)
        def other(db):
            db.execute("INSERT INTO sites(id,url,name,description_ja,description_en) VALUES('raced','https://alpha.example.jp/','Raced','競合','Raced')")
            db.execute("INSERT INTO site_categories VALUES('raced','arts')")
        self.d1.before_batch = other
        res = self.run_stage()
        self.assertEqual((res['published'], res['already_listed'], res['errors']), (0, 1, 0))
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites + 1)
        self.assertIsNone(self.d1.one('SELECT 1 FROM sites WHERE id=?', (ps.site_id('alpha.example.jp'),)))

    def test_published_row_not_reprocessed(self):
        add_candidate(self.d1)
        self.run_stage()
        n = self.d1.one('SELECT COUNT(*) FROM sites')[0]
        res = self.run_stage()
        self.assertEqual(res['selected'], 0)
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], n)

    def test_category_missing_in_d1_never_publishes_without_category(self):
        ps.VALID_CATEGORIES.add('ghost-cat')
        self.addCleanup(ps.VALID_CATEGORIES.discard, 'ghost-cat')
        ps.LEAF_CATEGORIES.add('ghost-cat')
        self.addCleanup(ps.LEAF_CATEGORIES.discard, 'ghost-cat')
        add_candidate(self.d1, categories=json.dumps(['ghost-cat']))
        res = self.run_stage()
        self.assertEqual((res['held'], res['published']), (1, 0))
        self.assertEqual(self.d1.one('SELECT review_reason FROM url_candidates')[0], 'gate:no_category')
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites)
        # the in-batch fail-safe alone also rolls everything back
        p = ps.plan(dict(self.d1.one('SELECT * FROM url_candidates')))
        with self.assertRaises(RuntimeError):
            self.d1.batch(ps.publish_statements(p))
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites)

    def test_port_443_duplicate_by_progress_key(self):
        self.d1.db.execute("INSERT INTO sites(id,url,name,description_ja,description_en) VALUES('manual-2','https://alpha.example.jp:443/','Hand','手書き','Hand')")
        self.d1.db.execute("INSERT INTO site_categories VALUES('manual-2','arts')")
        add_candidate(self.d1)
        res = self.run_stage()
        self.assertEqual((res['already_listed'], res['published']), (1, 0))
        self.assertEqual(self.d1.one('SELECT COUNT(*) FROM sites')[0], self.base_sites + 1)

    def test_bad_urls_are_gated(self):
        for i, u in enumerate(['https://user:pw@a.example.jp/', 'https://a.example.jp/ x', 'https://a.example.jp/\tx', 'ftp://a.example.jp/']):
            add_candidate(self.d1, f'bad{i}.example.jp', url=u, final_url=u)
        res = self.run_stage()
        self.assertEqual((res['held'], res['held_reasons']), (4, {'gate:no_url': 4}))

    def test_existing_site_adult_sync_only_from_null(self):
        self.d1.db.execute("INSERT INTO sites(id,url,name,description_ja,description_en,adult_content) VALUES('m-null','https://alpha.example.jp/','n','d','d',NULL)")
        self.d1.db.execute("INSERT INTO sites(id,url,name,description_ja,description_en,adult_content) VALUES('m-set','https://beta.example.jp/','n','d','d',0)")
        add_candidate(self.d1, adult='yes')
        add_candidate(self.d1, 'beta.example.jp', adult='yes')
        self.run_stage()
        self.assertEqual(self.d1.one("SELECT adult_content FROM sites WHERE id='m-null'")[0], 1)
        self.assertEqual(tuple(self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id='m-null' AND kind='thumbnail'")), ('excluded', 0))
        self.assertEqual(self.d1.one("SELECT adult_content FROM sites WHERE id='m-set'")[0], 0)   # never overwritten
        self.assertEqual(tuple(self.d1.one("SELECT state,eligible FROM site_media_jobs WHERE site_id='m-set' AND kind='thumbnail'")), ('pending', 1))

    def test_verify_failure_stops_the_loop(self):
        add_candidate(self.d1, 'a.example.jp')
        add_candidate(self.d1, 'b.example.jp')
        self.d1.db.execute("CREATE TRIGGER breakjobs AFTER INSERT ON site_media_jobs BEGIN DELETE FROM site_media_jobs WHERE rowid=NEW.rowid AND NEW.kind='favicon'; END")
        res = self.run_stage()
        self.assertTrue(res['stopped'])
        self.assertEqual(res['published'], 0)
        self.assertEqual(len(res['verify_problems']), 1)
        self.assertEqual(self.d1.one("SELECT COUNT(*) FROM url_progress WHERE state='approved'")[0], 1)   # the second one was not touched

    def test_failing_row_goes_to_back_of_queue(self):
        add_candidate(self.d1, 'a.example.jp', tags=json.dumps(['爆|boom']))
        add_candidate(self.d1, 'b.example.jp')
        self.d1.db.execute("UPDATE url_progress SET updated_at='2000-01-01 00:00:00' WHERE identity_key='a.example.jp/'")
        self.d1.db.execute("UPDATE url_progress SET updated_at='2001-01-01 00:00:00' WHERE identity_key='b.example.jp/'")
        self.d1.db.execute("CREATE TRIGGER poison BEFORE INSERT ON site_tags WHEN NEW.label_ja='爆' BEGIN SELECT RAISE(ABORT,'poison'); END")
        self.run_stage(limit=1)
        self.assertGreater(self.d1.one("SELECT updated_at FROM url_progress WHERE identity_key='a.example.jp/'")[0], '2000')
        res = self.run_stage(limit=1)
        self.assertEqual(res['published'], 1)   # b is now first

    def test_hold_does_not_touch_candidate_that_moved_on(self):
        add_candidate(self.d1, state='published', categories='[]')
        self.d1.batch(ps.hold_statements('alpha.example.jp/', 'gate:x'))
        self.assertIsNone(self.d1.one("SELECT review_reason FROM url_candidates WHERE identity_key='alpha.example.jp/'")[0])


if __name__ == '__main__':
    unittest.main()
