"""Canonical category sets, the one-off cleanup SQL and the data integrity checks (Issue #137). No network, no API.
All migrations are applied to an in-memory SQLite (the same way export_sites.load_db does), triggers of the count tables included."""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for _sub in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import contextlib  # noqa: E402
import io  # noqa: E402
import subprocess  # noqa: E402
import json  # noqa: E402

import export_approved_sql as ea  # noqa: E402
import integrity  # noqa: E402
import judge  # noqa: E402
import review_state  # noqa: E402
import test_llm_ledger as L  # noqa: E402
import test_update_categories as U  # noqa: E402

VALID = {c["id"] for c in judge.load_categories()}
LEGACY = judge.load_legacy_map()
RETREAT = ("business", "society", "business-services", "community")
FIX_SQL = open(os.path.join(ROOT, "scripts", "fix-redundant-categories.sql"), encoding="utf-8").read()
STATEMENTS = [s.strip() for s in "\n".join(l for l in FIX_SQL.splitlines() if not l.startswith("--")).split(";") if s.strip()]


def canon(ids, stats=None):
    return judge.modernize_categories(ids, VALID, LEGACY, stats=stats)


class NormalizeTests(unittest.TestCase):
    def test_parent_and_child_keep_the_child_only(self):
        self.assertEqual(canon(["health", "hospitals"]), ["hospitals"])
        self.assertEqual(canon(["hospitals", "health"]), ["hospitals"])  # order does not matter

    def test_three_levels_keep_the_deepest(self):
        self.assertEqual(canon(["humanities", "religion", "shrines"]), ["shrines"])
        self.assertEqual(canon(["humanities", "shrines"]), ["shrines"])  # a grandparent too, not only the direct parent

    def test_retreat_chosen_by_the_llm_is_kept_next_to_a_real_category(self):
        # business-services / community etc. are ordinary categories of the current tree (assign=llm): a new id is never dropped
        self.assertEqual(canon(["business-services", "manufacturing"]), ["business-services", "manufacturing"])
        self.assertEqual(canon(["jobs", "business-services"]), ["jobs", "business-services"])
        self.assertEqual(canon(["community", "hospitals"]), ["community", "hospitals"])
        self.assertEqual(canon(["society", "community"]), ["community"])  # society is the parent of community: the ancestor rule, not a retreat rule

    def test_old_id_fallback_is_used_only_when_nothing_else_remains(self):
        self.assertEqual(canon(["business-companies"]), ["business"])  # alone: the retreat id stays
        self.assertEqual(canon(["business-companies", "hospitals"]), ["hospitals"])  # a real category: the fallback is not added
        self.assertEqual(canon(["business-companies", "business-products-and-services"]), ["business-services"])  # business is the parent

    def test_ancestor_first_then_retreat(self):
        # business is the parent of business-services: only the child stays, which is a retreat id alone, so it stays
        self.assertEqual(canon(["business", "business-services"]), ["business-services"])
        self.assertEqual(canon(["business", "manufacturing"]), ["manufacturing"])

    def test_multiple_real_categories_and_the_limit(self):
        self.assertEqual(canon(["hospitals", "manufacturing", "health", "society"]), ["hospitals", "manufacturing", "society"])
        self.assertEqual(len(canon(["hospitals", "manufacturing", "agriculture", "construction"])), 3)

    def test_removal_makes_room_for_the_limit(self):
        # 4 ids but one is the parent of another: the 3 that are left all stay (cut after normalizing)
        self.assertEqual(canon(["health", "hospitals", "manufacturing", "agriculture"]), ["hospitals", "manufacturing", "agriculture"])

    def test_empty_and_invalid_stay_empty(self):
        self.assertEqual(canon([]), [])
        self.assertEqual(canon(["nope", "also-nope"]), [])

    def test_government_leaves_are_untouched_by_the_tree(self):
        # the rule leaves are not in the valid ids, so they never reach the set; the tree still knows them
        parent, _ = judge.load_tree()
        self.assertEqual(parent["cities"], "government")
        self.assertEqual(judge.normalize_categories(["government", "cities"]), ["cities"])

    def test_stats_count_dropped_ancestors_only(self):
        st = {}
        canon(["health", "hospitals", "society", "manufacturing"], st)
        self.assertEqual(st, {"dropped_ancestors": 1})

    def test_retreat_set_is_the_fallback_rows_of_the_map(self):
        self.assertEqual(set(RETREAT), set(judge.load_tree()[1]))

    def test_canonical_categories_keeps_unknown_for_validation(self):
        self.assertEqual(judge.canonical_categories(["health", "hospitals"], VALID, LEGACY), ["hospitals"])
        self.assertEqual(judge.canonical_categories(["health", "zzz"], VALID, LEGACY), ["health", "zzz"])
        self.assertEqual(judge.canonical_categories(["science-astronomy", "zzz"], VALID, LEGACY), ["astronomy", "zzz"])


def new_db():
    db = integrity.base_memory_db()
    for t in ("site_tags", "site_categories", "sites"):
        db.execute(f"DELETE FROM {t}")
    return db


def add(db, sid, cats, cc="JP", name="n", desc="d", url=None):
    db.execute("INSERT INTO sites (id,url,name,description_ja,country_code) VALUES (?,?,?,?,?)",
               (sid, url or f"https://{sid}.example/", name, desc, cc))
    db.executemany("INSERT INTO site_categories VALUES (?,?)", [(sid, c) for c in cats])


def snap(db):
    return db.execute("SELECT category_id,scope,n FROM category_counts ORDER BY 1,2").fetchall()


def found(db, only=None):
    return {k: v for k, v in integrity.run_checks(integrity.sqlite_execute(db)).items() if v and (only is None or k in only)}


class FixSqlTests(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.addCleanup(self.db.close)
        add(self.db, "s1", ["health", "hospitals"])
        add(self.db, "s2", ["business-services", "manufacturing"])  # retreat + real: both stay (the LLM may have chosen it)
        add(self.db, "s3", ["business", "society", "hospitals", "agriculture"])  # ancestor + retreat + real, 2 real
        add(self.db, "s4", ["society"], cc="US")  # retreat alone: stays
        add(self.db, "s5", ["hospitals"])  # already canonical
        add(self.db, "s6", ["humanities", "religion", "shrines"])  # 3 levels
        self.db.execute("INSERT INTO sites (id,url,name,description_ja,country_code) VALUES ('pb-9','https://pb9.example/','n','d','JP')")
        self.db.executemany("INSERT INTO site_categories VALUES ('pb-9',?)", [("government",), ("cities",)])  # a pb- with both: never touched
        self.db.commit()

    def test_statements_are_simple_single_deletes(self):
        self.assertEqual(len(STATEMENTS), 1)
        for s in STATEMENTS:
            self.assertTrue(s.startswith("DELETE FROM site_categories"))
            self.assertNotIn("UNION", s)

    def cats(self, sid):
        return sorted(r[0] for r in self.db.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)))

    def test_before_after(self):
        n0 = self.db.execute("SELECT count(*) FROM site_categories").fetchone()[0]
        before = found(self.db)
        # ancestor pairs: s1 1, s3 business>agriculture 1, s6 humanities>religion/shrines and religion>shrines 3;
        # retreat rows next to another category (information only): s2 1, s3 society and business 2
        self.assertEqual((len(before["ancestor_duplicate"]), len(before["retreat_with_real"])), (5, 3))
        counts = snap(self.db)
        self.db.executescript(FIX_SQL)
        n1 = self.db.execute("SELECT count(*) FROM site_categories").fetchone()[0]
        self.assertLess(n1, n0)
        self.assertEqual(self.cats("s1"), ["hospitals"])
        self.assertEqual(self.cats("s2"), ["business-services", "manufacturing"])  # untouched
        self.assertEqual(self.cats("s3"), ["agriculture", "hospitals", "society"])  # only the ancestor business goes
        self.assertEqual(self.cats("s4"), ["society"])
        self.assertEqual(self.cats("s5"), ["hospitals"])
        self.assertEqual(self.cats("s6"), ["shrines"])
        self.assertEqual(self.cats("pb-9"), ["cities", "government"])  # pb- untouched
        self.assertEqual(found(self.db, ("ancestor_duplicate", "uncategorized")), {})
        self.assertEqual(len(found(self.db, ("retreat_with_real",))["retreat_with_real"]), 2)  # s2 and s3: warn only, still there
        # the count tables follow by their triggers and still equal the recursive count
        self.assertEqual(found(self.db, [c[0] for c in integrity.CHECKS if "count" in c[0] or c[0] == "category_closure"]), {})
        # removing an ancestor row next to its descendant never changes any count: category_counts is identical row for row
        self.assertEqual(snap(self.db), counts)
        # a second run changes nothing
        self.db.executescript(FIX_SQL)
        self.assertEqual(self.db.execute("SELECT count(*) FROM site_categories").fetchone()[0], n1)

    def test_scope_counts_are_identical_too(self):
        scopes = self.db.execute("SELECT scope,n FROM scope_counts ORDER BY 1").fetchall()
        self.db.executescript(FIX_SQL)
        self.assertEqual(self.db.execute("SELECT scope,n FROM scope_counts ORDER BY 1").fetchall(), scopes)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.db = new_db()
        self.addCleanup(self.db.close)

    def test_clean_data_has_no_rows_except_warnings(self):
        add(self.db, "a", ["hospitals"])
        add(self.db, "b", ["manufacturing", "agriculture"], cc=None)
        self.assertEqual(found(self.db), {"no_country": [("b",)]})
        self.assertEqual({k: v for k, v in integrity.errors_of(found(self.db)).items() if v}, {})

    def test_the_real_migrations_data_is_clean(self):
        db = integrity.base_memory_db()
        self.addCleanup(db.close)
        self.assertEqual({k: v for k, v in integrity.errors_of(integrity.run_checks(integrity.sqlite_execute(db))).items() if v}, {})

    def test_each_check_detects_its_violation(self):
        db = self.db
        add(db, "anc", ["health", "hospitals"])
        add(db, "ret", ["community", "manufacturing"])
        db.execute("INSERT INTO sites (id,url,name,description_ja,country_code) VALUES ('none','https://none.example/','n','d','JP')")
        add(db, "dupa", ["hospitals"], url="https://Dup.example/x/")
        add(db, "dupb", ["hospitals"], url="http://www.dup.example/x")
        add(db, "empty", ["hospitals"], name=" ", desc="")
        add(db, "pb-1", ["government", "hospitals"])
        add(db, "pb-2", ["government"])  # government itself is not a leaf under government either
        add(db, "pb-3", ["cities"])  # a real leaf: fine
        add(db, "desc", ["hospitals"], name="named", desc=" ")  # only the Japanese description is empty
        add(db, "nj-empty", ["hospitals"], name="named")
        db.execute("UPDATE sites SET name_ja=' ' WHERE id='nj-empty'")  # name_ja が空白だけ(NULL か非空でなければならない)
        add(db, "ne-empty", ["hospitals"], name="named")
        db.execute("UPDATE sites SET name_en='' WHERE id='ne-empty'")
        add(db, "n-ok", ["hospitals"], name="named")
        db.execute("UPDATE sites SET name_ja='日本語名', name_en=NULL WHERE id='n-ok'")  # 片方だけ NULL は正常
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO site_categories VALUES ('anc','ghost')")
        db.execute("INSERT INTO site_categories VALUES ('ghost-site','hospitals')")
        got = found(db)
        self.assertEqual({r[0] for r in got["ancestor_duplicate"]}, {"anc"})
        self.assertEqual({r[0] for r in got["retreat_with_real"]}, {"ret"})
        self.assertEqual({r[0] for r in got["public_body_not_leaf"]}, {"pb-1", "pb-2"})
        self.assertEqual({r[0] for r in got["uncategorized"]}, {"none"})
        self.assertEqual({r[1] for r in got["orphan_site_categories"]}, {"ghost", "hospitals"})
        self.assertEqual([r[0] for r in got["duplicate_url"]], ["dup.example/x"])
        self.assertEqual({r[0] for r in got["empty_text"]}, {"empty", "desc", "nj-empty", "ne-empty"})

    def test_count_tables_are_detected_when_they_drift(self):
        add(self.db, "a", ["hospitals"])
        self.db.execute("DROP TRIGGER category_counts_sc_ad")  # a delete no longer follows
        self.db.execute("DELETE FROM site_categories WHERE site_id='a'")
        got = found(self.db)
        self.assertIn("category_counts_all", got)
        self.assertIn("category_counts_country", got)
        self.db.execute("UPDATE scope_counts SET n=99 WHERE scope='all'")
        self.db.execute("DELETE FROM category_closure WHERE ancestor_id='health' AND descendant_id='hospitals'")
        got = found(self.db)
        self.assertIn("scope_counts_all", got)
        self.assertIn("category_closure", got)

    def test_duplicate_pair_check_runs(self):
        add(self.db, "a", ["hospitals"])  # the primary key makes a real duplicate impossible: the check stays at 0 rows
        self.assertEqual(found(self.db, ("duplicate_site_category",)), {})

    def test_every_check_is_one_simple_select(self):
        for name, _sev, _why, sql in integrity.CHECKS:
            self.assertLessEqual(sql.count("UNION"), 2, name)  # D1 production: few compound terms
            self.assertNotIn(";", sql, name)


class GateTests(unittest.TestCase):
    def test_good_sql_passes_and_bad_sql_is_caught(self):
        ok = ("INSERT INTO sites (id,url,name,description_ja,country_code) VALUES ('g1','https://g1.example/','n','d','JP');"
              "INSERT INTO site_categories VALUES ('g1','hospitals');")
        self.assertEqual(integrity.gate(ok), {})
        bad = ok + "INSERT INTO site_categories VALUES ('g1','health');"
        found_ = integrity.gate(bad)
        self.assertEqual(list(found_), ["ancestor_duplicate"])
        self.assertEqual(integrity.gate("INSERT INTO nope VALUES (1);").keys(), {"sql_error"})

    def test_violations_the_base_already_has_do_not_block(self):
        db = integrity.base_memory_db()
        add(db, "old", ["health", "hospitals"])
        db.commit()
        path = os.path.join(tempfile.gettempdir(), f"integrity-gate-{os.getpid()}.sqlite")
        try:
            disk = sqlite3.connect(path)
            db.backup(disk)
            disk.close()
            self.assertEqual(integrity.gate("SELECT 1;", path), {})
            self.assertEqual(list(integrity.gate("INSERT INTO site_categories VALUES ('old','medicine');", path)), ["ancestor_duplicate"])
        finally:
            db.close()
            if os.path.exists(path):
                os.remove(path)


class PipelineTests(L.Base):
    """import_results / export_approved_sql end to end: the canonical form on the way in, the gate on the way out."""

    def approve(self, key, cats):
        self.add(key)
        self.imp(self.write_pipeline({key: L.out_pass(categories=cats)}, name=f"{key}.json"))
        review_state.set_state(self.db, [key], "approved")

    def export(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ea.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_import_stores_the_canonical_form(self):
        self.add("a.example")
        counts, problems = self.imp(self.write_pipeline({"a.example": L.out_pass(categories=["health", "hospitals", "society"])}, name="c.json"))
        self.assertEqual((counts["imported"], problems), (1, []))
        self.assertEqual(json.loads(self.row("a.example")["categories"]), ["hospitals", "society"])  # society is kept: not an old-id fallback

    def test_export_writes_the_canonical_form_and_the_gate_passes(self):
        self.approve("a.example", ["health", "hospitals", "business-services"])
        # a judgement stored before this change (not canonical) is normalized again at export time; the retreat id stays (LLM choice)
        self.db.execute("UPDATE llm_judgement SET categories=? WHERE key='a.example'", (json.dumps(["health", "hospitals", "business-services"]),))
        self.db.commit()
        out = self.path("a.sql")
        rc, _o, err = self.export("--db", self.path("f.sqlite"), "--out", out)
        self.assertEqual(rc, 0, err)
        t = integrity.base_memory_db()
        self.addCleanup(t.close)
        t.executescript(L.read(out))
        self.assertEqual(sorted(r[0] for r in t.execute("SELECT category_id FROM site_categories WHERE site_id LIKE 'fr-a-%'")), ["business-services", "hospitals"])
        self.assertIn("dropped_ancestors=1", err)
        self.assertNotIn("dropped_retreat", err)

    def test_export_stops_without_writing_when_the_sql_would_break_integrity(self):
        self.approve("a.example", ["hospitals"])
        out = self.path("a.sql")
        orig = ea.statements
        # force a violation: a category row that points at no category (an orphan) is injected into the SQL
        ea.statements = lambda row, norm=None: orig(row, norm) + ["INSERT OR IGNORE INTO site_categories VALUES ('fr-a-example-xx','x')"]
        try:
            rc, _o, err = self.export("--db", self.path("f.sqlite"), "--out", out)
        finally:
            ea.statements = orig
        self.assertEqual(rc, 3)
        self.assertIn("INTEGRITY", err)
        self.assertFalse(os.path.exists(out))


class UpdateModeGateTests(U.UpdateBase):
    """--update-categories: the gate only works with --current (without it the base has no fr- site and the UPDATE changes nothing)."""

    def inject(self):
        """A violation in the generated SQL: alpha gets its category's parent next to the child."""
        orig = ea.build_update
        sid = self.ids["alpha.example"]
        ea.build_update = lambda items, current=None: (lambda r: (r[0] + f"INSERT OR IGNORE INTO site_categories VALUES ('{sid}','science');\n", r[1]))(orig(items, current))
        self.addCleanup(setattr, ea, "build_update", orig)

    def test_gate_stops_the_update_with_current(self):
        self.inject()
        out = self.path("u.sql")
        rc, _o, err = self.run_main("--update-categories", "--db", self.dbp, "--current", self.curpath, "--out", out)
        self.assertEqual(rc, 3, err)
        self.assertIn("INTEGRITY", err)
        self.assertIn("ancestor_duplicate", err)
        self.assertFalse(os.path.exists(out))

    def test_clean_update_passes_with_current(self):
        out = self.path("u.sql")
        rc, _o, err = self.run_main("--update-categories", "--db", self.dbp, "--current", self.curpath, "--out", out)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("WARNING", err)
        self.assertTrue(os.path.exists(out))

    def test_without_current_a_warning_says_the_gate_is_blind(self):
        out = self.path("u.sql")
        rc, _o, err = self.run_main("--update-categories", "--db", self.dbp, "--out", out)
        self.assertEqual(rc, 0, err)
        self.assertIn("WARNING: no --current", err)


class CliExitCodeTests(unittest.TestCase):
    """scripts/check-data-integrity.py: exit 1 on an error-level violation, 0 when only warnings remain."""

    def run_cli(self, setup):
        db = new_db()
        setup(db)
        db.commit()
        path = os.path.join(tempfile.gettempdir(), f"integrity-cli-{os.getpid()}.sqlite")
        try:
            disk = sqlite3.connect(path)
            db.backup(disk)
            disk.close()
            p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check-data-integrity.py"), "--sqlite", path],
                               capture_output=True, text=True)
            return p.returncode, p.stdout
        finally:
            db.close()
            if os.path.exists(path):
                os.remove(path)

    def test_error_exits_1(self):
        rc, out = self.run_cli(lambda db: add(db, "a", ["health", "hospitals"]))
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] ancestor_duplicate", out)

    def test_warnings_only_exit_0(self):
        def setup(db):
            add(db, "a", ["business-services", "manufacturing"])  # retreat next to another: warn
            add(db, "b", ["hospitals"], cc=None)  # no country: warn
        rc, out = self.run_cli(setup)
        self.assertEqual(rc, 0, out)
        self.assertIn("[warn] retreat_with_real", out)
        self.assertIn("[warn] no_country", out)

    def test_clean_exits_0(self):
        rc, out = self.run_cli(lambda db: add(db, "a", ["hospitals"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("all error-level checks passed", out)


if __name__ == "__main__":
    unittest.main()
