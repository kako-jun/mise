"""export_approved_sql.py --update-categories / --update-categories-from (Issue #122). No network, no API.
All migrations are applied to an in-memory SQLite (the same way export_sites.load_db does), the generated SQL is run on it."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for sub in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import export_approved_sql as ea  # noqa: E402
import counts_check  # noqa: E402
import export_sites  # noqa: E402
import review_state  # noqa: E402
import test_llm_ledger as L  # noqa: E402


def cats_of(db, sid):
    return sorted(r[0] for r in db.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)))


_BASE = None


def base_db():
    """All migrations applied once per run (it is slow); every test gets its own copy."""
    global _BASE
    if _BASE is None:
        _BASE = export_sites.load_db(ROOT)
    copy = sqlite3.connect(":memory:")
    _BASE.backup(copy)
    return copy


def tearDownModule():
    global _BASE
    if _BASE is not None:
        _BASE.close()
        _BASE = None


class UpdateBase(L.Base):
    def setUp(self):
        super().setUp()
        self.ids = {k: ea.site_id(k) for k in ("alpha.example", "beta.example", "gamma.example", "delta.example")}
        outs = {k: L.out_pass() for k in ("alpha.example", "beta.example", "gamma.example")}
        outs["delta.example"] = L.out_pass()
        self.imp(self.write_pipeline(outs))
        review_state.set_state(self.db, ["alpha.example", "beta.example", "gamma.example"], "approved")
        self.set_cats("alpha.example", ["astronomy", "photography"])
        self.set_cats("beta.example", ["science-astronomy", "company"])  # old ids (pre-#114): translated through category-map
        self.set_cats("gamma.example", [])  # empty judgement
        self.dbp = os.path.join(self.dir, "f.sqlite")
        self.target = base_db()
        self.addCleanup(self.target.close)
        t = self.target
        for k in ("alpha.example", "beta.example", "gamma.example", "delta.example"):
            t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES (?,?,?,?)", (self.ids[k], f"https://{k}/", k, "d"))
        for k, c in (("alpha.example", "business"), ("beta.example", "society"), ("gamma.example", "community"), ("delta.example", "business")):
            t.execute("INSERT INTO site_categories VALUES (?,?)", (self.ids[k], c))
        t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES ('manual-1','https://manual.example/','m','d')")
        t.execute("INSERT INTO site_categories VALUES ('manual-1','business')")
        t.commit()
        self.curpath = self.path("current.sqlite")
        t.execute("VACUUM INTO ?", (self.curpath,))

    def set_cats(self, key, cats):
        self.db.execute("UPDATE llm_judgement SET categories=? WHERE key=?", (json.dumps(cats), key))
        self.db.commit()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ea.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def run_sql(self, path):
        self.target.executescript(L.read(path))


class LedgerModeTests(UpdateBase):
    def test_replaces_categories_and_is_idempotent(self):
        out = self.path("u.sql")
        rc, so, _ = self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.assertEqual(rc, 0)
        self.run_sql(out)
        t, i = self.target, self.ids
        self.assertEqual(cats_of(t, i["alpha.example"]), ["astronomy", "photography"])
        self.assertEqual(cats_of(t, i["beta.example"]), ["astronomy"])  # old company -> only as a fallback, dropped here
        self.assertEqual(cats_of(t, i["gamma.example"]), ["community"])  # empty judgement: untouched
        self.assertEqual(cats_of(t, i["delta.example"]), ["business"])  # not approved: untouched
        self.assertEqual(cats_of(t, "manual-1"), ["business"])  # not a frontier id
        snap = list(t.execute("SELECT * FROM site_categories ORDER BY 1,2"))
        self.run_sql(out)
        self.assertEqual(list(t.execute("SELECT * FROM site_categories ORDER BY 1,2")), snap)

    def test_counts_table_follows_the_category_replacement(self):
        """migration 0022 のトリガ: --update-categories の DELETE + INSERT OR IGNORE のあとも集計表 = 再帰 CTE（#126）。"""
        out = self.path("u2.sql")
        self.assertEqual(self.run_main("--update-categories", "--db", self.dbp, "--out", out)[0], 0)
        counts_check.check(self, self.target)
        self.run_sql(out)
        counts_check.check(self, self.target)
        self.run_sql(out)  # 冪等
        counts_check.check(self, self.target)

    def test_not_listed_site_is_not_inserted(self):
        self.target.execute("DELETE FROM site_categories WHERE site_id=?", (self.ids["alpha.example"],))
        self.target.execute("DELETE FROM sites WHERE id=?", (self.ids["alpha.example"],))
        out = self.path("u.sql")
        self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.run_sql(out)
        self.assertEqual(cats_of(self.target, self.ids["alpha.example"]), [])

    def test_government_leaf_and_pb_are_left_alone(self):
        t = self.target
        t.execute("DELETE FROM site_categories WHERE site_id=?", (self.ids["alpha.example"],))
        t.execute("INSERT INTO site_categories VALUES (?, 'cities')", (self.ids["alpha.example"],))
        out = self.path("u.sql")
        self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.run_sql(out)
        self.assertEqual(cats_of(t, self.ids["alpha.example"]), ["cities"])
        before = cats_of(t, "pb-010006")
        self.assertTrue(before)
        pb_snap = list(t.execute("SELECT * FROM site_categories WHERE site_id LIKE 'pb-%' ORDER BY 1,2"))
        self.assertNotIn("'pb-0", L.read(out))
        self.assertEqual(list(t.execute("SELECT * FROM site_categories WHERE site_id LIKE 'pb-%' ORDER BY 1,2")), pb_snap)
        self.assertEqual(cats_of(t, "pb-010006"), before)

    def test_sql_guards_pb_ids_itself(self):
        t = self.target
        before = cats_of(t, "pb-010006")
        sql = "\n".join(ea.update_statements("pb-010006", ["astronomy"]))
        t.executescript(sql)
        self.assertEqual(cats_of(t, "pb-010006"), before)

    def test_unknown_new_ids_never_leave_a_site_uncategorised(self):
        t = self.target
        sid = self.ids["alpha.example"]
        t.executescript("\n".join(ea.update_statements(sid, ["no-such-category", "also-missing"])))
        self.assertEqual(cats_of(t, sid), ["business"])
        t.executescript("\n".join(ea.update_statements(sid, ["astronomy", "no-such-category"])))
        self.assertEqual(cats_of(t, sid), ["astronomy"])

    def test_explicit_out_is_respected_in_update_mode(self):
        out = self.path("mine.sql")
        self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.assertTrue(os.path.exists(out))

    def test_site_without_categories_gets_them(self):
        self.target.execute("DELETE FROM site_categories WHERE site_id=?", (self.ids["alpha.example"],))
        out = self.path("u.sql")
        self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.run_sql(out)
        self.assertEqual(cats_of(self.target, self.ids["alpha.example"]), ["astronomy", "photography"])

    def test_dry_run_writes_nothing_and_summarises(self):
        out = self.path("u.sql")
        rc, so, _ = self.run_main("--update-categories", "--db", self.dbp, "--out", out, "--dry-run", "--current", self.curpath)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(out))
        self.assertIn("sites changed: 2", so)  # alpha, beta
        self.assertIn("having a real category: 2", so)  # business -> astronomy/photography, society -> astronomy
        self.assertIn("astronomy: 2", so)
        self.assertIn("business: 1", so)
        self.assertIn("skipped_empty=1", so)  # gamma

    def test_current_leaves_out_government_leaf_and_unlisted(self):
        t = self.target
        t.execute("DELETE FROM site_categories WHERE site_id=?", (self.ids["alpha.example"],))
        t.execute("INSERT INTO site_categories VALUES (?, 'cities')", (self.ids["alpha.example"],))
        t.execute("DELETE FROM site_categories WHERE site_id=?", (self.ids["beta.example"],))
        t.execute("DELETE FROM sites WHERE id=?", (self.ids["beta.example"],))
        t.commit()
        cur = self.path("c2.sqlite")
        t.execute("VACUUM INTO ?", (cur,))
        rc, so, _ = self.run_main("--update-categories", "--db", self.dbp, "--dry-run", "--current", cur)
        self.assertIn("not listed: 1", so)
        self.assertIn("government leaf): 1", so)
        self.assertIn("sites changed: 0", so)

    def test_modes_do_not_mix(self):
        self.assertEqual(self.run_main("--dry-run", "--db", self.dbp)[0], 1)
        self.assertEqual(self.run_main("--update-categories", "--update-categories-from", "x.json", "--db", self.dbp)[0], 1)


class ResultsModeTests(UpdateBase):
    def setUp(self):
        super().setUp()
        t = self.target
        for sid, c in (("sample-a", "business"), ("sample-b", "society"), ("sample-c", "business"), ("sample-d", "business")):
            t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES (?,?,?,?)", (sid, f"https://{sid}.example/", sid, "d"))
            t.execute("INSERT INTO site_categories VALUES (?,?)", (sid, c))
        t.commit()
        t.execute("VACUUM INTO ?", (self.path("current2.sqlite"),))
        self.curpath = self.path("current2.sqlite")
        self.cands = self.path("c.jsonl")
        with open(self.cands, "w", encoding="utf-8") as f:
            for sid in ("sample-a", "sample-b", "sample-c", "sample-d", "pb-010006"):
                f.write(json.dumps({"id": sid, "url": f"https://{sid}.example/", "title": sid, "meta_description": "m", "lang": "ja",
                                    "body_excerpt": L.BODY, "features": {}}, ensure_ascii=False) + "\n")

    def results(self, outs):
        p = self.path("r.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"args": {"model": "haiku", "describe_model": None, "stages": "two", "body_limit": 9000},
                       "results": [{"id": k, "output": o} for k, o in outs.items()]}, f, ensure_ascii=False)
        return p

    def test_pass_only_validated_and_pb_excluded(self):
        bad = L.out_pass(categories=["nope"])
        outs = {"sample-a": L.out_pass(categories=["astronomy"]),
                "sample-b": L.out_pass(categories=["science-astronomy"]),  # old id
                "sample-c": L.out_reject(), "sample-d": bad,
                "pb-010006": L.out_pass(categories=["astronomy"])}
        sql_path = self.path("r.sql")
        rc, so, err = self.run_main("--update-categories-from", self.results(outs), "--input", self.cands, "--out", sql_path, "--current", self.curpath)
        self.assertEqual(rc, 2)  # sample-d rejected
        self.assertIn("sample-d: invalid output", err)
        self.run_sql(sql_path)
        t = self.target
        self.assertEqual(cats_of(t, "sample-a"), ["astronomy"])
        self.assertEqual(cats_of(t, "sample-b"), ["astronomy"])
        self.assertEqual(cats_of(t, "sample-c"), ["business"])  # reject: untouched
        self.assertEqual(cats_of(t, "sample-d"), ["business"])  # invalid: untouched
        self.assertNotIn("pb-010006", L.read(sql_path))
        self.assertIn("skipped_protected=1", so)
        self.assertIn("skipped_verdict=1", so)
        snap = list(t.execute("SELECT * FROM site_categories ORDER BY 1,2"))
        self.run_sql(sql_path)
        self.assertEqual(list(t.execute("SELECT * FROM site_categories ORDER BY 1,2")), snap)

    def test_pre_129_result_without_freshness_is_accepted(self):
        old = L.out_pass(categories=["astronomy"])
        old.pop("freshness", None); old.pop("exempt", None)
        sql_path = self.path("old.sql")
        rc, so, err = self.run_main("--update-categories-from", self.results({"sample-a": old}), "--input", self.cands, "--out", sql_path, "--current", self.curpath)
        self.assertEqual(rc, 0, err)
        self.run_sql(sql_path)
        self.assertEqual(cats_of(self.target, "sample-a"), ["astronomy"])

    def test_fr_ids_are_left_to_the_ledger_mode(self):
        t = self.target
        fr = self.ids["delta.example"]  # listed, business only
        with open(self.cands, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": fr, "url": "https://delta.example/", "title": "d", "meta_description": "m", "lang": "ja",
                                "body_excerpt": L.BODY, "features": {}}, ensure_ascii=False) + "\n")
        outs = {"sample-a": L.out_pass(categories=["astronomy"]), fr: L.out_pass(categories=["astronomy"])}
        sql_path = self.path("r.sql")
        rc, so, _ = self.run_main("--update-categories-from", self.results(outs), "--input", self.cands, "--out", sql_path, "--current", self.curpath)
        self.assertEqual(rc, 0)
        self.assertIn("skipped_ledger_mode=1", so)
        self.assertNotIn(fr, L.read(sql_path))
        self.run_sql(sql_path)
        self.assertEqual(cats_of(t, fr), ["business"])
        self.assertEqual(cats_of(t, "sample-a"), ["astronomy"])

    def test_url_mismatch_with_the_listed_site_is_left_out(self):
        t = self.target
        t.execute("UPDATE sites SET url='https://other.example/' WHERE id='sample-a'")
        t.commit()
        cur = self.path("c3.sqlite")
        t.execute("VACUUM INTO ?", (cur,))
        outs = {"sample-a": L.out_pass(categories=["astronomy"]), "sample-b": L.out_pass(categories=["astronomy"])}
        sql_path = self.path("r.sql")
        rc, so, err = self.run_main("--update-categories-from", self.results(outs), "--input", self.cands, "--out", sql_path, "--current", cur)
        self.assertEqual(rc, 0)
        self.assertIn("skipped_url_mismatch=1", so)
        self.assertIn("sample-a: candidate URL", err)
        self.run_sql(sql_path)
        self.assertEqual(cats_of(t, "sample-a"), ["business"])
        self.assertEqual(cats_of(t, "sample-b"), ["astronomy"])

    def test_unlisted_site_is_not_created_and_input_is_required(self):
        outs = {"sample-a": L.out_pass(categories=["astronomy"])}
        r = self.results(outs)
        self.target.execute("DELETE FROM site_categories WHERE site_id='sample-a'")
        self.target.execute("DELETE FROM sites WHERE id='sample-a'")
        sql_path = self.path("r.sql")
        self.run_main("--update-categories-from", r, "--input", self.cands, "--out", sql_path)
        self.run_sql(sql_path)
        self.assertEqual(cats_of(self.target, "sample-a"), [])
        self.assertEqual(self.run_main("--update-categories-from", r)[0], 1)


if __name__ == "__main__":
    unittest.main()
