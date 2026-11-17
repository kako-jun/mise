"""Key-less tests for the ledger round trip: import_results -> review_state -> export_approved_sql (Issue #48).
No network, no API. Run: uv run python3 -W error::ResourceWarning -m unittest discover -s tests"""
import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import export_approved_sql  # noqa: E402
import export_candidates  # noqa: E402
import export_sites  # noqa: E402
import frontier  # noqa: E402
import import_results  # noqa: E402
import review_state  # noqa: E402

BODY = "東京都に事務所のある小さな団体です。活動の記録を公開しています。"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def out_pass(**kw):
    o = {
        "description_ja": "小さな団体のサイト。", "description_en": "The site of a small organization.",
        "long_description_ja": "東京都にある小さな団体が活動の記録を公開している。", "long_description_en": "A small organization in Tokyo publishes records of its activities.",
        "long_description_evidence": ["活動の記録を公開しています"],
        "spam": {"verdict": "pass", "kind": "none", "reason": "Plain organization page."},
        "categories": ["business-organizations"], "tags": ["organization", "records"],
        "adult": {"value": "no", "evidence_quote": None},
        "site_scope": "standalone", "site_kind": "organization", "site_kind_reason": "Says it is an organization.",
        "confidence": "medium",
        "location": {"country_code": "JP", "region_code": "JP-13", "evidence_quote": "東京都に事務所のある"},
        "notes": "",
    }
    o.update(kw)
    return o


def out_reject():
    o = out_pass(description_ja=None, description_en=None, long_description_ja=None, long_description_en=None,
                 long_description_evidence=[], categories=[], tags=[])
    o["spam"] = {"verdict": "reject", "kind": "advertising", "reason": "Only ads."}
    o["location"] = {"country_code": None, "region_code": None, "evidence_quote": None}
    return o


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.db = frontier.connect(os.path.join(self.dir, "f.sqlite"))
        for key in ("alpha.example", "beta.example", "gamma.example", "delta.example"):
            self.add(key)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, key, title=None, **cols):
        frontier.add(self.db, key, key, f"https://{key}/", "links", state="candidate")
        self.db.execute("UPDATE frontier SET title=?, description=?, text_head=?, lang='ja' WHERE key=?",
                        (title if title is not None else f"{key} title", "meta", BODY, key))
        for c, v in cols.items():
            self.db.execute(f"UPDATE frontier SET {c}=? WHERE key=?", (v, key))
        self.db.commit()

    def path(self, name):
        return os.path.join(self.dir, name)

    def write_input(self):
        p = self.path("in.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for c in export_candidates.export(os.path.join(self.dir, "f.sqlite")):
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        return p

    def write_pipeline(self, outputs, name="res.json", model="haiku", describe=None):
        p = self.path(name)
        res = [{"id": export_candidates.cand_id(k), "output": o, **({"triage": o} if o and o["spam"]["verdict"] != "pass" else {})}
               for k, o in outputs.items()]
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"args": {"model": model, "describe_model": describe, "stages": "two", "body_limit": 9000},
                       "results": res}, f, ensure_ascii=False)
        return p

    def imp(self, results, inp=None):
        return import_results.import_results(self.db, results, inp or self.write_input())

    def row(self, key):
        return self.db.execute("SELECT * FROM llm_judgement WHERE key=?", (key,)).fetchone()


class ImportTests(Base):
    def test_imports_pipeline_output_with_every_field(self):
        c, probs = self.imp(self.write_pipeline({"alpha.example": out_pass()}, describe="sonnet"))
        self.assertEqual((c["imported"], c["rejected"], probs), (1, 0, []))
        r = self.row("alpha.example")
        self.assertEqual(r["spam_verdict"], "pass")
        self.assertEqual(r["description_ja"], "小さな団体のサイト。")
        self.assertEqual(json.loads(r["categories"]), ["business-organizations"])
        self.assertEqual(json.loads(r["tags"]), ["organization", "records"])
        self.assertEqual(json.loads(r["long_description_evidence"]), ["活動の記録を公開しています"])
        self.assertEqual((r["country_code"], r["region_code"], r["confidence"], r["site_kind"], r["site_scope"]),
                         ("JP", "JP-13", "medium", "organization", "standalone"))
        self.assertEqual(r["model"], "haiku+sonnet")
        self.assertEqual((r["origin"], r["review_state"]), ("pipeline", "pending"))
        self.assertTrue(r["input_hash"] and r["judged_at"] and r["imported_at"])
        self.assertEqual(self.db.execute("SELECT state FROM frontier WHERE key='alpha.example'").fetchone()[0], "candidate")

    def test_triage_only_item_uses_the_first_model(self):
        self.imp(self.write_pipeline({"alpha.example": out_reject()}, model="haiku", describe="sonnet"))
        self.assertEqual(self.row("alpha.example")["model"], "haiku")

    def test_judge_list_format(self):
        p = self.path("judge.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{"id": "beta_example", "model": "claude-sonnet-5-5", "output": out_pass()},
                       {"id": "gamma_example", "model": "claude-sonnet-5-5", "output": None, "error": "bad"}], f)
        c, _ = self.imp(p)
        self.assertEqual((c["imported"], c["no_output"]), (1, 1))
        self.assertEqual((self.row("beta.example")["origin"], self.row("beta.example")["model"]), ("judge", "claude-sonnet-5-5"))

    def test_same_input_hash_is_not_imported_twice_and_keeps_review_state(self):
        p = self.write_pipeline({"alpha.example": out_pass()})
        self.imp(p)
        review_state.set_state(self.db, ["alpha.example"], "approved")
        c, _ = self.imp(p)
        self.assertEqual((c["unchanged"], c["imported"], c["updated"]), (1, 0, 0))
        self.assertEqual(self.row("alpha.example")["review_state"], "approved")
        self.assertEqual(self.db.execute("SELECT count(*) FROM llm_judgement").fetchone()[0], 1)

    def test_new_hash_overwrites_and_returns_to_pending(self):
        self.imp(self.write_pipeline({"alpha.example": out_pass()}, model="haiku"))
        review_state.set_state(self.db, ["alpha.example"], "approved")
        c, _ = self.imp(self.write_pipeline({"alpha.example": out_pass(description_ja="書き直した紹介文。")}, name="r2.json", model="sonnet"))
        self.assertEqual((c["updated"], c["imported"]), (1, 0))
        r = self.row("alpha.example")
        self.assertEqual((r["description_ja"], r["model"], r["review_state"], r["reviewed_at"]), ("書き直した紹介文。", "sonnet", "pending", None))

    def test_changed_candidate_text_changes_the_hash(self):
        p = self.write_pipeline({"alpha.example": out_pass()})
        self.imp(p)
        self.db.execute("UPDATE frontier SET text_head=? WHERE key='alpha.example'", (BODY + "追記。",))
        self.db.commit()
        c, _ = self.imp(p)
        self.assertEqual(c["updated"], 1)

    def test_unknown_host_is_rejected(self):
        p = self.path("res.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"args": {"model": "haiku"}, "results": [{"id": "nowhere_example", "output": out_pass()}]}, f)
        inp = self.write_input()
        with open(inp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "nowhere_example", "url": "https://nowhere.example/", "title": "t", "meta_description": "m", "lang": "ja", "body_excerpt": BODY, "features": {}}) + "\n")
        c, probs = self.imp(p, inp)
        self.assertEqual(c["rejected"], 1)
        self.assertIn("no matching host", probs[0])
        self.assertEqual(self.db.execute("SELECT count(*) FROM llm_judgement").fetchone()[0], 0)

    def test_ambiguous_id_is_rejected(self):
        self.add("a_b.example")
        self.add("a.b.example")  # both map to a_b_example
        c, probs = self.imp(self.write_pipeline({"a_b.example": out_pass()}))
        self.assertEqual(c["rejected"], 1)
        self.assertIn("ambiguous", probs[0])

    def test_invalid_outputs_are_rejected_not_written(self):
        bad_quote = out_pass(long_description_evidence=["原文に無い引用"])
        bad_cat = out_pass(categories=["no-such-category"])
        bad_keys = {"description_ja": "x"}
        inp = self.write_input()
        p = self.write_pipeline({"alpha.example": bad_quote, "beta.example": bad_cat, "gamma.example": out_pass()})
        data = json.loads(read(p))
        data["results"].append({"id": "delta_example", "output": bad_keys})
        write(p, json.dumps(data))
        c, probs = self.imp(p, inp)
        self.assertEqual((c["imported"], c["rejected"]), (1, 3))
        self.assertEqual(self.db.execute("SELECT key FROM llm_judgement").fetchall()[0][0], "gamma.example")

    def test_stale_input_url_is_rejected(self):
        inp = self.write_input()
        self.db.execute("UPDATE frontier SET final_url='https://moved.example/' WHERE key='alpha.example'")
        self.db.commit()
        c, probs = self.imp(self.write_pipeline({"alpha.example": out_pass()}), inp)
        self.assertEqual(c["rejected"], 1)
        self.assertIn("stale", probs[0])

    def test_malformed_files_exit_with_message(self):
        p = self.path("bad.json")
        write(p, "{not json")
        with self.assertRaises(SystemExit):
            self.imp(p)
        write(p, '{"foo": 1}')
        with self.assertRaises(SystemExit):
            self.imp(p)

    def test_reject_is_auto_rejected_and_state_untouched(self):
        self.imp(self.write_pipeline({"alpha.example": out_reject()}))
        r = self.row("alpha.example")
        self.assertEqual((r["spam_verdict"], r["review_state"]), ("reject", "rejected"))
        self.assertEqual(self.db.execute("SELECT state FROM frontier WHERE key='alpha.example'").fetchone()[0], "candidate")

    def test_deferred_is_pending(self):
        o = out_pass(site_scope="user_page")
        o["spam"] = {"verdict": "deferred", "kind": "none", "reason": "A user page."}
        self.imp(self.write_pipeline({"alpha.example": o}))
        self.assertEqual(self.row("alpha.example")["review_state"], "pending")

    def test_adult_yes_keeps_no_body_derived_text(self):
        o = out_pass(adult={"value": "yes", "evidence_quote": "活動の記録を公開しています"})
        self.imp(self.write_pipeline({"alpha.example": o}))
        r = self.row("alpha.example")
        self.assertEqual(r["adult"], "yes")
        self.assertIsNone(r["long_description_ja"]); self.assertIsNone(r["long_description_en"]); self.assertIsNone(r["long_description_evidence"])
        self.assertNotIn("活動の記録を公開しています", json.dumps(dict(r), ensure_ascii=False))

    def test_cli_exit_codes(self):
        inp = self.write_input()
        good = self.write_pipeline({"alpha.example": out_pass()})
        dbp = os.path.join(self.dir, "f.sqlite")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(import_results.main([good, "--input", inp, "--db", dbp]), 0)
            self.assertEqual(import_results.main([self.write_pipeline({"alpha.example": out_pass(categories=["zzz"])}, name="b.json"), "--input", inp, "--db", dbp]), 2)


class ReviewTests(Base):
    def setUp(self):
        super().setUp()
        self.imp(self.write_pipeline({"alpha.example": out_pass(), "beta.example": out_reject(),
                                      "gamma.example": out_pass(description_ja=None, description_en=None, long_description_ja=None,
                                                                long_description_en=None, long_description_evidence=[])}))

    def test_approve_reject_pending_roundtrip(self):
        self.assertEqual(review_state.set_state(self.db, ["WWW.Alpha.example"], "approved")[0][1], True)
        self.assertEqual(self.row("alpha.example")["review_state"], "approved")
        self.assertIsNotNone(self.row("alpha.example")["reviewed_at"])
        review_state.set_state(self.db, ["alpha.example"], "rejected")
        self.assertEqual(self.row("alpha.example")["review_state"], "rejected")
        review_state.set_state(self.db, ["alpha.example"], "pending")
        self.assertEqual((self.row("alpha.example")["review_state"], self.row("alpha.example")["reviewed_at"]), ("pending", None))

    def test_refusals(self):
        res = dict((k, (ok, m)) for k, ok, m in review_state.set_state(self.db, ["beta.example", "gamma.example", "delta.example"], "approved"))
        self.assertFalse(res["beta.example"][0]); self.assertIn("not listable", res["beta.example"][1])
        self.assertFalse(res["gamma.example"][0]); self.assertIn("description", res["gamma.example"][1])
        self.assertFalse(res["delta.example"][0]); self.assertIn("no judgement", res["delta.example"][1])

    def test_list_filters(self):
        self.assertEqual([r["key"] for r in review_state.list_rows(self.db, "pending", "pass")], ["alpha.example", "gamma.example"])
        self.assertEqual([r["key"] for r in review_state.list_rows(self.db, "rejected")], ["beta.example"])

    def test_cli(self):
        dbp = os.path.join(self.dir, "f.sqlite")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(review_state.main(["--db", dbp, "approve", "alpha.example"]), 0)
            self.assertEqual(review_state.main(["--db", dbp, "approve", "beta.example"]), 1)
            self.assertEqual(review_state.main(["--db", dbp, "list", "--state", "approved"]), 0)


class SchemaFollowTests(unittest.TestCase):
    def test_old_ledger_gets_the_table_on_connect(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "old.sqlite")
            schema = read(os.path.join(ROOT, "scripts", "pipeline", "schema.sql"))
            c = sqlite3.connect(p)
            c.executescript(schema.split("-- LLM 判定結果")[0])  # a ledger from before llm_judgement existed
            c.execute("INSERT INTO frontier (key,host,url,source,discovered_at) VALUES ('x.example','x.example','https://x.example/','links','t')")
            c.commit()
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='llm_judgement'").fetchone()[0], 0)
            c.close()
            db = frontier.connect(p)
            cols = [r[1] for r in db.execute("PRAGMA table_info(llm_judgement)")]
            self.assertIn("review_state", cols)
            self.assertEqual(db.execute("SELECT count(*) FROM frontier").fetchone()[0], 1)
            db.close()
            db = frontier.connect(p)  # second connect: no error, nothing lost
            self.assertEqual(db.execute("SELECT count(*) FROM frontier").fetchone()[0], 1)
            db.close()

    def test_table_survives_the_frontier_rebuild(self):
        """An old frontier without 'deferred' is rebuilt on connect; llm_judgement rows must stay."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "old.sqlite")
            db = frontier.connect(p)
            frontier.add(db, "x.example", "x.example", "https://x.example/", "links", state="candidate")
            db.execute("INSERT INTO llm_judgement (key,input_hash,origin,model,judged_at,imported_at,spam_verdict,spam_kind,spam_reason,categories,tags,adult,site_kind,site_kind_reason,site_scope,confidence) "
                       "VALUES ('x.example','h','judge','m','t','t','pass','none','r','[]','[]','no','unknown','r','standalone','low')")
            db.commit()
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='frontier'").fetchone()[0]
            db.executescript("ALTER TABLE frontier RENAME TO f2; CREATE TABLE frontier AS SELECT * FROM f2; DROP TABLE f2;")  # a table without the constraint text
            db.close()
            db = frontier.connect(p)
            self.assertEqual(db.execute("SELECT count(*) FROM llm_judgement").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM frontier").fetchone()[0], 1)
            db.close()


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.add("alpha.example", title="  Alpha   Site  ")
        self.add("adultsite.example", title="Explicit title", adult="yes")
        self.add("o'brien.example", title="O'Brien's page")
        outs = {"alpha.example": out_pass(), "adultsite.example": out_pass(adult={"value": "yes", "evidence_quote": "活動の記録を公開しています"}),
                "o'brien.example": out_pass(tags=["it's"]), "beta.example": out_pass(), "gamma.example": out_pass(), "delta.example": out_reject()}
        self.imp(self.write_pipeline(outs))
        review_state.set_state(self.db, ["alpha.example", "adultsite.example", "o'brien.example", "gamma.example"], "approved")
        self.sqlpath = self.path("out.sql")
        self.dbp = os.path.join(self.dir, "f.sqlite")

    def export(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export_approved_sql.main(["--db", self.dbp, "--out", self.sqlpath]), 0)
        return read(self.sqlpath)

    def target(self):
        t = export_sites.load_db(ROOT)
        self.addCleanup(t.close)
        return t

    def test_only_approved_are_exported_and_sql_is_clean(self):
        sql = self.export()
        for k in ("alpha.example", "adultsite.example", "o'brien.example", "gamma.example"):
            self.assertIn(f"-- {k}", sql)
        for k in ("beta.example", "delta.example"):
            self.assertNotIn(f"-- {k}", sql)
        self.assertNotIn("--remote", sql)
        self.assertNotIn("DELETE", sql); self.assertNotIn("UPDATE", sql)

    def test_applies_once_and_again_adds_nothing(self):
        sql = self.export()
        t = self.target()
        before = t.execute("SELECT count(*) FROM sites").fetchone()[0]
        t.executescript(sql)
        added = t.execute("SELECT count(*) FROM sites").fetchone()[0] - before
        self.assertEqual(added, 4)
        counts = [t.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in ("sites", "site_categories", "site_tags")]
        t.executescript(sql)
        self.assertEqual(counts, [t.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in ("sites", "site_categories", "site_tags")])
        r = t.execute("SELECT name, description_ja, description_en, country_code, region_code, registered_at, bulk_import FROM sites WHERE url='https://alpha.example/'").fetchone()
        self.assertEqual(r[:5], ("Alpha Site", "小さな団体のサイト。", "The site of a small organization.", "JP", "JP-13"))
        self.assertTrue(r[5]); self.assertEqual(r[6], 0)
        sid = t.execute("SELECT id FROM sites WHERE url='https://alpha.example/'").fetchone()[0]
        self.assertEqual(t.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)).fetchall(), [("business-organizations",)])
        self.assertEqual(sorted(x[0] for x in t.execute("SELECT label_en FROM site_tags WHERE site_id=?", (sid,))), ["organization", "records"])
        self.assertEqual(t.execute("SELECT name FROM sites WHERE url='https://adultsite.example/'").fetchone()[0], "adultsite.example")
        self.assertEqual(t.execute("SELECT name FROM sites WHERE url='https://o''brien.example/'").fetchone()[0], "O'Brien's page")
        self.assertEqual(t.execute("SELECT count(*) FROM site_tags WHERE label_ja=?", ("it's",)).fetchone()[0], 1)

    def test_already_listed_host_is_excluded_even_with_other_spelling(self):
        t = self.target()
        t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES ('old','http://WWW.Alpha.example','a','d')")
        t.executescript(self.export())
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%alpha.example%'").fetchone()[0], 1)
        self.assertEqual(t.execute("SELECT count(*) FROM site_categories WHERE site_id LIKE 'fr-alpha%'").fetchone()[0], 0)

    def test_blocked_host_and_parent_domain_are_excluded(self):
        t = self.target()
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('alpha.example','','removal_request')")
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('example','x','manual')")  # path rule on another host: no effect
        t.executescript(self.export())
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%alpha.example%'").fetchone()[0], 0)
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%gamma.example%'").fetchone()[0], 1)

    def test_subdomain_of_blocked_domain_is_excluded(self):
        self.add("sub.blocked.example")
        self.imp(self.write_pipeline({"sub.blocked.example": out_pass()}, name="r3.json"))
        review_state.set_state(self.db, ["sub.blocked.example"], "approved")
        t = self.target()
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('blocked.example','','manual')")
        t.executescript(self.export())
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%blocked.example%'").fetchone()[0], 0)

    def test_duplicate_urls_inside_the_approved_set_are_exported_once(self):
        self.add("dup.example", final_url="https://alpha.example/")
        self.imp(self.write_pipeline({"dup.example": out_pass()}, name="r4.json"))
        review_state.set_state(self.db, ["dup.example"], "approved")
        # final_url differs from the input url: refresh the input so the import accepts it
        sql = self.export()
        t = self.target()
        t.executescript(sql)
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url='https://alpha.example/'").fetchone()[0], 1)

    def test_unapproved_after_reimport_drops_out(self):
        p = self.write_pipeline({"alpha.example": out_pass(description_ja="別の紹介文。")}, name="r5.json", model="sonnet")
        self.imp(p)  # new hash -> pending again
        self.assertNotIn("-- alpha.example", self.export())

    def test_missing_ledger_or_table(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export_approved_sql.main(["--db", self.path("none.sqlite"), "--out", self.sqlpath]), 1)
            c = sqlite3.connect(self.path("bare.sqlite")); c.execute("CREATE TABLE frontier (key TEXT)"); c.close()
            self.assertEqual(export_approved_sql.main(["--db", self.path("bare.sqlite"), "--out", self.sqlpath]), 1)


if __name__ == "__main__":
    unittest.main()
