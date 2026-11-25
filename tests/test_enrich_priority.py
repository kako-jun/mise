"""優先順(scripts/enrich/priority.py)。D1 は実マイグレーション全件から作った in-memory SQLite。ネットワーク・本番には出ない。"""
import datetime
import glob
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
sys.path.insert(0, str(ROOT / 'scripts' / 'enrich'))
from enrich import priority as pr
from enrich import fetch_stage as fs
import judge_stage as js

T0 = datetime.datetime(2026, 11, 24, 12, 0, 0)


_TEMPLATE = []


def template():
    """全 migration を 1 回だけ流した DB(テストごとの再実行は遅いので backup で複製する)。シードされたサイトの所属は空にする。"""
    if not _TEMPLATE:
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE d1_migrations(id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)')
        for f in sorted(glob.glob(str(ROOT / 'migrations' / '*.sql'))):
            db.executescript(Path(f).read_text(encoding='utf-8'))
        db.execute('DELETE FROM site_categories')
        db.commit()
        _TEMPLATE.append(db)
    return _TEMPLATE[0]


class FakeD1:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        template().backup(self.db)
        self.db.row_factory = sqlite3.Row
        self.max_params, self.writes = 0, 0
        leaves = [r[0] for r in self.db.execute("SELECT id FROM categories WHERE id NOT IN (SELECT parent_id FROM categories WHERE parent_id IS NOT NULL) ORDER BY id")]
        self.leaves = leaves

    def query(self, sql, params=()):
        params = list(params)
        self.max_params = max(self.max_params, len(params))
        assert len(params) <= 100, 'more than 100 bound parameters'
        if not sql.lstrip().upper().startswith('SELECT'):
            self.writes += 1
        rows = [dict(r) for r in self.db.execute(sql, params).fetchall()]
        self.db.commit()
        return rows

    def batch(self, stmts):
        return [self.query(s, p) for s, p in stmts]

    def publish(self, leaf, n):
        self.db.executemany('INSERT INTO site_categories(site_id,category_id) VALUES(?,?)', [(f'{leaf}-{i}', leaf) for i in range(n)])
        self.db.commit()

    def add(self, url, state='discovered', hint=None, cats=None):
        from progress_identity import progress_identity_key
        k = progress_identity_key(url)
        self.db.execute('INSERT INTO url_progress(identity_key,url,state,updated_at) VALUES(?,?,?,?)', (k, url, state, '2026-11-24 00:00:00'))
        if hint is not None or cats is not None or state == 'fetched':
            self.db.execute('INSERT INTO url_candidates(identity_key,host,url,state,source,discovered_at,depth,hint_leaf,categories) VALUES(?,?,?,?,?,?,?,?,?)',
                            (k, url.split('/')[2], url, state, 'wikidata', '2026-11-24 00:00:00', 0, hint, cats))
        self.db.commit()
        return k


class Deficits(unittest.TestCase):
    def setUp(self):
        self.d = FakeD1()
        self.a, self.b, self.c = self.d.leaves[:3]

    def test_leaves_only_and_all_present(self):
        defs = pr.leaf_deficits(self.d)
        self.assertEqual(sorted(defs), [l for l in self.d.leaves if l not in pr.rule_leaf_ids()])   # assign=rule の葉は含めない
        self.assertFalse(set(defs) & pr.rule_leaf_ids())
        self.assertTrue(all(v == pr.FLOOR for v in defs.values()))

    def test_published_and_inflight_reduce_deficit(self):
        self.d.publish(self.a, 40)
        self.d.publish(self.b, 150)
        self.d.add('https://x1.example/', 'fetched', hint=self.c)
        self.d.add('https://x2.example/', 'queued', hint=self.c)
        self.d.add('https://x3.example/', 'discovered', hint=self.c)           # not in flight
        self.d.add('https://x4.example/', 'reviewing', hint=self.a, cats=f'["{self.c}"]')   # judged first category wins
        defs = pr.leaf_deficits(self.d)
        self.assertEqual(defs[self.a], 60)
        self.assertEqual(defs[self.b], 0)
        self.assertEqual(defs[self.c], 100 - 3)

    def test_rank_and_inflight_use_same_leaf(self):
        """判定済み(categories 先頭)と hint が食い違う行は、処理中の数えも rank も categories 先頭の葉で揃う。"""
        k = self.d.add('https://mix.example/', 'fetched', hint=self.a, cats=f'["{self.c}"]')
        defs = pr.leaf_deficits(self.d)
        self.assertEqual((defs[self.a], defs[self.c]), (100, 99))
        rank, _ = pr.rank_sql(defs)
        r = self.d.query(f"SELECT {rank} AS r FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.identity_key=?", [k])
        self.assertEqual(r[0]['r'], 0)   # c は不足 99 の葉 = rank 0(hint の a で見ていても 0 だが、c を full にすると下で 2 になる)
        self.d.publish(self.c, 100)
        defs = pr.leaf_deficits(self.d)
        rank, _ = pr.rank_sql(defs)
        r = self.d.query(f"SELECT {rank} AS r FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.identity_key=?", [k])
        self.assertEqual(r[0]['r'], 2)

    def test_invalid_id_refused(self):
        with self.assertRaises(ValueError):
            pr.rank_sql({"x'; DROP TABLE sites;--": 5})

    def test_rank_sql_no_bound_params_and_ranks(self):
        defs = {self.a: 10, self.b: 0, self.c: 90}
        rank, dsql = pr.rank_sql(defs, 'h')
        rows = self.d.query(f"SELECT h, {rank} AS r, {dsql} AS d FROM (SELECT ? AS h UNION ALL SELECT ? UNION ALL SELECT ? UNION ALL SELECT NULL UNION ALL SELECT 'not-a-leaf')",
                            [self.a, self.b, self.c])
        by = {r['h']: (r['r'], r['d']) for r in rows}
        self.assertEqual(by[self.a], (0, 10)); self.assertEqual(by[self.c], (0, 90))
        self.assertEqual(by[self.b][0], 2); self.assertEqual(by[None][0], 1); self.assertEqual(by['not-a-leaf'][0], 1)
        full = pr.rank_sql(pr.leaf_deficits(self.d))
        self.assertNotIn('?', ''.join(full))


class Selection(unittest.TestCase):
    def setUp(self):
        self.d = FakeD1()
        self.a, self.b, self.c = self.d.leaves[:3]
        self.d.publish(self.c, 120)            # c already full
        self.d.publish(self.b, 70)             # b deficit 30, a deficit 100
        self.k = {
            'full': self.d.add('https://aa-full.example/', hint=self.c),
            'nohint': self.d.add('https://ab-nohint.example/'),
            'small': self.d.add('https://ac-small.example/', hint=self.b),
            'big': self.d.add('https://zz-big.example/', hint=self.a),
        }

    def keys(self, rows):
        inv = {v: n for n, v in self.k.items()}
        return [inv[r['identity_key']] for r in rows]

    def test_fetch_order_rank_then_deficit(self):
        sel = pr.Selector('leaf')
        rows = fs.select_rows(self.d, 10, T0, sel)
        self.assertEqual(self.keys(rows), ['big', 'small', 'nohint', 'full'])   # nothing dropped
        self.assertEqual(self.d.max_params, 3)

    def test_plain_progress_rows_without_candidate_are_rank1_in_key_order(self):
        d = FakeD1()
        keys = [d.add(f'https://plain{i}.example/') for i in range(5)]   # url_candidates 行なし
        sel, out = pr.Selector('leaf'), []
        rows = fs.select_rows(d, 10, T0, sel)
        sel.report(rows, out.append, 'fetch')
        self.assertEqual([r['identity_key'] for r in rows], sorted(keys))
        self.assertTrue(all(r['prio_rank'] == 1 for r in rows))
        self.assertIn('rank0/1/2=0/5/0', out[0])

    def test_existing_hint_rows_rank0_and_rank2(self):
        """url_candidates に hint_leaf が既にある行(旧 frontier 由来)では rank 0/2 が効き、ヒント無しの行は間に入る。"""
        rows = fs.select_rows(self.d, 10, T0, pr.Selector('leaf'))
        self.assertEqual([r['prio_rank'] for r in rows], [0, 0, 1, 2])

    def test_none_and_default_are_identity_key_order(self):
        plain = self.keys(fs.select_rows(self.d, 10, T0))
        self.assertEqual(plain, self.keys(sorted(fs.select_rows(self.d, 10, T0), key=lambda r: r['identity_key'])))
        self.assertEqual(self.keys(fs.select_rows(self.d, 10, T0, pr.Selector('none'))), plain)
        self.assertNotIn('prio_rank', fs.select_rows(self.d, 1, T0)[0])

    def test_limit_bounded_takes_rank0_first(self):
        rows = fs.select_rows(self.d, 2, T0, pr.Selector('leaf'))
        self.assertEqual(self.keys(rows), ['big', 'small'])

    def test_fairness_every_fifth_batch(self):
        sel = pr.Selector('leaf')
        plain = self.keys(sorted(fs.select_rows(self.d, 10, T0), key=lambda r: r['identity_key']))
        orders = [self.keys(fs.select_rows(self.d, 10, T0, sel)) for _ in range(5)]
        self.assertEqual(orders[0], ['big', 'small', 'nohint', 'full'])
        self.assertEqual(orders[3], orders[0])
        self.assertEqual(orders[4], plain)      # 5th batch: identity_key order only
        self.assertNotEqual(plain, orders[0])

    def test_report_counts_and_top5(self):
        sel, out = pr.Selector('leaf'), []
        rows = fs.select_rows(self.d, 10, T0, sel)
        sel.report(rows, out.append, 'fetch')
        self.assertIn('rank0/1/2=2/1/1', out[0])
        self.assertIn(self.a, out[0])

    def test_fetch_run_dry_writes_nothing_and_no_files(self):
        before = self.d.writes
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as t:
            os.chdir(t)
            try:
                out = []
                stats = fs.run(self.d, limit=10, apply=False, log=out.append, now=lambda: T0, priority=pr.Selector('leaf'))
                self.assertEqual(os.listdir(t), [])
            finally:
                os.chdir(cwd)
        self.assertEqual(stats['selected'], 4)
        self.assertEqual(self.d.writes, before)
        self.assertTrue(any('priority' in x for x in out))

    def test_judge_selection_order(self):
        for name, hint in (('full', self.c), ('nohint', None), ('small', self.b), ('big', self.a)):
            url = f'https://j-{name}.example/'
            self.k['j' + name] = self.d.add(url, 'fetched', hint=hint)
        sel, out = pr.Selector('leaf'), []
        stats = js.run(self.d, limit=2, apply=False, log=out.append, priority=sel)
        self.assertEqual(stats['selected'], 2)
        line = [x for x in out if x.startswith('judge priority')][0]
        self.assertIn('rank0/1/2=2/0/0', line)
        order, extra = pr.Selector('leaf').order(self.d, 'p.identity_key')
        rows = self.d.query(f"SELECT p.identity_key{extra} FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='fetched' ORDER BY {order}")
        self.assertEqual(self.keys(rows), ['jbig', 'jsmall', 'jnohint', 'jfull'])


if __name__ == '__main__':
    unittest.main()
