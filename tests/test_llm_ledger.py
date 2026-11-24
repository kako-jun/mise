"""Key-less tests for the ledger round trip: import_results -> review_state -> export_approved_sql (Issue #48).
No network, no API. Run: uv run python3 -W error::ResourceWarning -m unittest discover -s tests"""
import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for sub in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import counts_check  # noqa: E402
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
        "categories": ["welfare"], "tags": ["organization", "records"],
        "adult": {"value": "no", "evidence_quote": None},
        "site_scope": "standalone", "site_kind": "organization", "site_kind_reason": "Says it is an organization.", "freshness": "active", "exempt": "none",
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


class LegacyImportTests(Base):
    def test_results_with_old_category_ids_are_imported_as_new_ids(self):
        key = "oldids.example"
        self.add(key)
        counts, problems = self.imp(self.write_pipeline({key: out_pass(categories=["science-astronomy", "arts-photography"])}, name="old.json"))
        self.assertEqual((counts["imported"], problems), (1, []))
        self.assertEqual(json.loads(self.row(key)["categories"]), ["astronomy", "photography"])

    def test_unknown_ids_are_still_rejected(self):
        key = "bad.example"
        self.add(key)
        counts, problems = self.imp(self.write_pipeline({key: out_pass(categories=["nope", "science-astronomy"])}, name="bad.json"))
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("unknown category ids", problems[0])


class ImportTests(Base):
    def test_imports_pipeline_output_with_every_field(self):
        c, probs = self.imp(self.write_pipeline({"alpha.example": out_pass()}, describe="sonnet"))
        self.assertEqual((c["imported"], c["rejected"], probs), (1, 0, []))
        r = self.row("alpha.example")
        self.assertEqual(r["spam_verdict"], "pass")
        self.assertEqual(r["description_ja"], "小さな団体のサイト。")
        self.assertEqual(json.loads(r["categories"]), ["welfare"])
        self.assertEqual(json.loads(r["tags"]), ["organization", "records"])
        self.assertEqual(json.loads(r["long_description_evidence"]), ["活動の記録を公開しています"])
        self.assertEqual((r["country_code"], r["region_code"], r["confidence"], r["site_kind"], r["site_scope"]),
                         ("JP", "JP-13", "medium", "organization", "standalone"))
        self.assertEqual((r["freshness"], r["exempt"], r["review_reason"]), ("active", "none", None))
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


class ImportSafetyTests(Base):
    def test_body_limit_of_the_judge_run_decides_the_hash_and_validation(self):
        long_body = "東京都に事務所のある" + "あ" * 200 + "活動の記録を公開しています"
        self.db.execute("UPDATE frontier SET text_head=? WHERE key='alpha.example'", (long_body,))
        self.db.commit()
        inp = self.write_input()
        res = [{"id": export_candidates.cand_id("alpha.example"), "model": "m", "output": out_pass(long_description_evidence=["東京都に事務所のある"]), "body_limit": 25}]
        p = self.path("j.json")
        write(p, json.dumps(res))
        self.assertEqual(self.imp(p, inp)[0]["imported"], 1)
        h1 = self.row("alpha.example")["input_hash"]
        res[0]["body_limit"] = 9000
        res[0]["output"]["long_description_evidence"] = ["活動の記録を公開しています"]
        write(p, json.dumps(res))
        self.assertEqual(self.imp(p, inp)[0]["updated"], 1)
        self.assertNotEqual(self.row("alpha.example")["input_hash"], h1)
        # an old output without body_limit falls back to judge.BODY_LIMIT
        del res[0]["body_limit"]
        write(p, json.dumps(res))
        self.assertEqual(self.imp(p, inp)[0]["unchanged"], 1)
        # a quote beyond the limit the judge actually used is rejected
        res[0]["body_limit"] = 25
        write(p, json.dumps(res))
        counts, problems = self.imp(p, inp)
        self.assertEqual(counts["rejected"], 1, problems)

    def test_older_results_file_cannot_overwrite_a_newer_judgement(self):
        newer = self.write_pipeline({"alpha.example": out_pass(description_ja="新しい紹介文。")}, name="new.json", model="sonnet")
        older = self.write_pipeline({"alpha.example": out_pass(description_ja="古い紹介文。")}, name="old.json", model="haiku")
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_800_000_000, 1_800_000_000))
        self.imp(newer)
        review_state.set_state(self.db, ["alpha.example"], "approved")
        counts, problems = self.imp(older)
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("older", problems[0])
        self.assertEqual(self.row("alpha.example")["description_ja"], "新しい紹介文。")
        self.assertEqual(self.row("alpha.example")["review_state"], "approved")
        counts, _ = import_results.import_results(self.db, older, self.write_input(), allow_older=True)
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(self.row("alpha.example")["review_state"], "pending")


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
        self.assertNotIn("DELETE", sql)
        self.assertIn("UPDATE sites SET adult_content=1", sql)

    def test_approved_adult_classification_is_exported_to_d1_for_media_eligibility(self):
        sql = self.export()
        self.assertIn('region_code,adult_content)', sql)
        t = self.target()
        t.executescript(sql)
        self.assertEqual(t.execute("SELECT adult_content FROM sites WHERE url='https://adultsite.example/'").fetchone()[0], 1)
        self.assertEqual(t.execute("SELECT eligible,state FROM site_media_jobs WHERE site_id=(SELECT id FROM sites WHERE url='https://adultsite.example/') AND kind='thumbnail'").fetchone(), (0, 'excluded'))
        self.assertEqual(t.execute("SELECT eligible,state FROM site_media_jobs WHERE site_id=(SELECT id FROM sites WHERE url='https://adultsite.example/') AND kind='favicon'").fetchone(), (1, 'pending'))

    def test_export_synchronizes_adult_yes_for_an_already_published_site(self):
        t = self.target()
        t.execute("INSERT INTO sites(id,url,name,description_ja) VALUES('existing-adult','https://adultsite.example/','existing','説明')")
        t.executescript(self.export())
        self.assertEqual(t.execute("SELECT adult_content FROM sites WHERE id='existing-adult'").fetchone()[0], 1)
        self.assertEqual(t.execute("SELECT eligible,state FROM site_media_jobs WHERE site_id='existing-adult' AND kind='thumbnail'").fetchone(), (0, 'excluded'))

    def test_exported_unknown_classification_clears_adult_yes_and_reenables_thumbnail(self):
        t = self.target()
        t.execute("INSERT INTO sites(id,url,name,description_ja,adult_content) VALUES('existing-adult','https://adultsite.example/','existing','説明',1)")
        self.db.execute("UPDATE llm_judgement SET adult='unknown' WHERE key='adultsite.example'")
        self.db.commit()
        sql = self.export()
        self.assertIn("UPDATE sites SET adult_content=NULL", sql)
        self.assertIn("UPDATE site_media_jobs SET eligible=1", sql)
        t.executescript(sql)
        self.assertIsNone(t.execute("SELECT adult_content FROM sites WHERE id='existing-adult'").fetchone()[0])
        self.assertEqual(t.execute("SELECT eligible,state FROM site_media_jobs WHERE site_id='existing-adult' AND kind='thumbnail'").fetchone(), (1, 'pending'))

    def test_counts_table_follows_the_nightly_insert(self):
        """migration 0022 のトリガ: 夜間の一括投入（通常 INSERT）のあとも集計表 = 再帰 CTE（#126）。"""
        t = self.target()
        counts_check.check(self, t)
        sql = self.export()
        t.executescript(sql)
        counts_check.check(self, t)
        t.executescript(sql)  # 2 回目（INSERT OR IGNORE）で二重に数えない
        counts_check.check(self, t)
        self.assertEqual(t.execute("SELECT n FROM scope_counts WHERE scope='all'").fetchone()[0], t.execute("SELECT COUNT(*) FROM sites").fetchone()[0])

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
        self.assertEqual(t.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)).fetchall(), [("welfare",)])
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

    def listed(self, t, host):
        return t.execute("SELECT id FROM sites WHERE url LIKE ?", (f"%{host}%",)).fetchone()

    def approve_new(self, key, out, name="rx.json", **cols):
        self.add(key, **cols)
        self.imp(self.write_pipeline({key: out}, name=name))
        review_state.set_state(self.db, [key], "approved")

    def counts(self, t):
        return [t.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in ("sites", "site_categories", "site_tags")]

    def test_old_category_ids_in_the_ledger_are_exported_as_new_ids(self):
        # #114 より前に台帳へ入った判定は旧 59 カテゴリの id を持つ。書き出しで新しい木の id に写し、カテゴリ無しで載らないようにする
        self.approve_new("legacy.example", out_pass(), name="r-legacy.json")
        self.db.execute("UPDATE llm_judgement SET categories=? WHERE key='legacy.example'",
                        (json.dumps(["business-companies", "science-astronomy", "recreation-travel"]),))
        self.db.commit()
        sql = self.export()
        self.assertNotIn("science-astronomy", sql); self.assertNotIn("business-companies", sql)
        t = self.target()
        t.executescript(sql)
        got = sorted(r[0] for r in t.execute("SELECT category_id FROM site_categories WHERE site_id LIKE 'fr-legacy%'"))
        self.assertEqual(got, ["astronomy", "travel"])  # 運営主体の「企業」は、ほかに残る id があるので足されない

    def test_reapproved_site_that_is_already_listed_gets_no_new_children(self):
        t = self.target()
        t.executescript(self.export())
        sid = t.execute("SELECT id FROM sites WHERE url='https://alpha.example/'").fetchone()[0]
        before = (t.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)).fetchall(),
                  sorted(t.execute("SELECT label_en FROM site_tags WHERE site_id=?", (sid,)).fetchall()))
        # new model -> new hash -> pending; approve again; different categories and tags
        p = self.write_pipeline({"alpha.example": out_pass(categories=["arts", "photography", "architecture"],
                                                           tags=["a", "b", "c", "d", "e"])}, name="rr.json", model="sonnet")
        self.imp(p)
        self.assertEqual(review_state.set_state(self.db, ["alpha.example"], "approved")[0][1], True)
        c = self.counts(t)
        t.executescript(self.export())
        self.assertEqual(self.counts(t), c)
        self.assertEqual((t.execute("SELECT category_id FROM site_categories WHERE site_id=?", (sid,)).fetchall(),
                          sorted(t.execute("SELECT label_en FROM site_tags WHERE site_id=?", (sid,)).fetchall())), before)

    def test_new_site_gets_at_most_3_categories_and_5_tags_and_each_alone_works(self):
        self.approve_new("many.example", out_pass(categories=["photography", "architecture", "news"],  # not "arts": a parent next to its child is dropped (#137)
                                                  tags=["T1", "t2", "t3", "t4", "t5"]))
        self.approve_new("onlycat.example", out_pass(categories=["news"], tags=[]), name="r6.json")
        self.approve_new("onlytag.example", out_pass(categories=[], tags=["solo"]), name="r7.json")
        t = self.target()
        sql = self.export()
        t.executescript(sql)
        c = self.counts(t)
        t.executescript(sql)
        self.assertEqual(self.counts(t), c)
        n = lambda tbl, host: t.execute(f"SELECT count(*) FROM {tbl} WHERE site_id=?", (self.listed(t, host)[0],)).fetchone()[0]
        self.assertEqual((n("site_categories", "many.example"), n("site_tags", "many.example")), (3, 5))
        self.assertEqual((n("site_categories", "onlycat.example"), n("site_tags", "onlycat.example")), (1, 0))
        self.assertIsNone(self.listed(t, "onlytag.example"))  # no category at all: not listed (#137, uncategorized is a violation)

    def test_tags_are_deduplicated_case_insensitively_and_length_capped(self):
        rows = export_approved_sql.clean_tags(["Photo", "photo", " PHOTO ", "x" * 41, "a  b", "c", "d", "e", "f"])
        self.assertEqual(rows, ["Photo", "a b", "c", "d", "e"])

    def test_rejected_recommendation_and_approved_removal_are_excluded_pending_is_not(self):
        for k in ("rej.example", "rm.example", "pend.example", "rmpend.example", "addok.example", "rmno.example"):
            self.approve_new(k, out_pass(), name=f"r-{k}.json")
        t = self.target()
        ins = "INSERT INTO requests (type,status,url,host,normalized_key) VALUES (?,?,?,?,?)"
        t.execute(ins, ("add", "rejected", "https://www.Rej.example/", "rej.example", "rej.example"))
        t.execute(ins, ("remove", "approved", "https://rm.example/", "rm.example", "rm.example"))
        t.execute(ins, ("add", "pending", "https://pend.example/", "pend.example", "pend.example"))
        t.execute(ins, ("remove", "pending", "https://rmpend.example/", "rmpend.example", "rmpend.example"))
        t.execute(ins, ("add", "approved", "https://addok.example/", "addok.example", "addok.example"))
        t.execute(ins, ("remove", "rejected", "https://rmno.example/", "rmno.example", "rmno.example"))
        t.executescript(self.export())
        for host, listed in (("rej.example", 0), ("rm.example", 0), ("pend.example", 1), ("rmpend.example", 1),
                             ("addok.example", 1), ("rmno.example", 1)):
            self.assertEqual(1 if self.listed(t, host) else 0, listed, host)
        self.assertEqual(t.execute("SELECT count(*) FROM site_tags WHERE site_id LIKE 'fr-rej-%'").fetchone()[0], 0)

    def test_path_prefix_block_excludes_only_that_path_and_folds_the_path(self):
        self.approve_new("sakura.example/~bob", out_pass(), name="r8.json", url="https://sakura.example/%7Ebob/")
        self.approve_new("sakura.example/alice", out_pass(), name="r9.json", url="https://sakura.example/alice/")
        t = self.target()
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('sakura.example','/~bob','removal_request')")
        sql = self.export()
        t.executescript(sql)
        self.assertIsNone(t.execute("SELECT 1 FROM sites WHERE url LIKE '%bob%'").fetchone())
        self.assertIsNotNone(t.execute("SELECT 1 FROM sites WHERE url LIKE '%alice%'").fetchone())

    def test_blocked_sql_matches_the_intake_rules(self):
        t = self.target()
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('sakura.example','/~bob','manual')")
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('a_c.example','','manual')")
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('github.io','','manual')")
        def hit(url):
            return t.execute("SELECT " + export_approved_sql.blocked_sql(url)).fetchone()[0]
        self.assertEqual(hit("https://sakura.example/%7Ebob/page"), 1)
        self.assertEqual(hit("https://www.sakura.example//~Bob/;x"), 1)
        self.assertEqual(hit("https://sakura.example/~bobby"), 0)
        self.assertEqual(hit("https://sub.sakura.example/~bob"), 0)
        self.assertEqual(hit("https://abc.example/"), 0)       # _ is not a wildcard
        self.assertEqual(hit("https://a_c.example/"), 1)
        self.assertEqual(hit("https://x.y.a_c.example/"), 1)    # deep subdomain of a blocked domain
        self.assertEqual(hit("https://user.github.io/"), 0)     # shared hosting: the user area only, like the intake

    def test_listed_comparison_ignores_only_a_leading_www(self):
        self.assertEqual(export_approved_sql.norm_url("http://WWW.awww.site.com/a/"), "awww.site.com/a")
        self.assertEqual(export_approved_sql.norm_url("https://awww.site.com/x"), "awww.site.com/x")
        t = self.target()
        for u, want in (("https://awww.site.com/x/", "awww.site.com/x"), ("http://www.Site.com/%7Ea", "site.com/~a")):
            self.assertEqual(t.execute("SELECT " + export_approved_sql.norm_sql(export_approved_sql.q(u))).fetchone()[0], want)

    def test_listed_site_with_tilde_spelling_is_excluded(self):
        self.approve_new("tilde.example/~u", out_pass(), name="r10.json", url="https://tilde.example/%7Eu/")
        t = self.target()
        t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES ('old','http://www.tilde.example/~u','a','d')")
        t.executescript(self.export())
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%tilde.example%'").fetchone()[0], 1)

    def test_redirected_final_url_path_does_not_hide_a_removal_or_a_listing(self):
        # (a) approved removal recorded for the registered root; the site redirects / -> /ja/
        self.approve_new("foo.example", out_pass(), name="rd1.json", final_url="https://foo.example/ja/")
        # (b) listed under the registered root; the ledger final_url is /index.html
        self.approve_new("bar.example", out_pass(), name="rd2.json", final_url="https://bar.example/index.html")
        # a different path on the same host, nothing listed or requested: still exported
        self.approve_new("baz.example", out_pass(), name="rd3.json", final_url="https://baz.example/ja/")
        t = self.target()
        t.execute("INSERT INTO requests (type,status,url,host,normalized_key) VALUES ('remove','approved','https://foo.example/','foo.example','foo.example')")
        t.execute("INSERT INTO sites (id,url,name,description_ja) VALUES ('old','https://bar.example/','b','d')")
        t.execute("INSERT INTO requests (type,status,url,host,normalized_key) VALUES ('remove','approved','https://baz.example/other','baz.example','baz.example/other')")
        t.executescript(self.export())
        self.assertIsNone(self.listed(t, "foo.example"))
        self.assertEqual(t.execute("SELECT count(*) FROM sites WHERE url LIKE '%bar.example%'").fetchone()[0], 1)
        self.assertIsNotNone(self.listed(t, "baz.example"))

    def test_final_url_key_and_url_are_all_checked_for_requests_and_path_blocks(self):
        self.approve_new("qux.example", out_pass(), name="rd4.json", final_url="https://qux.example/ja/")
        self.approve_new("sub.example/a", out_pass(), name="rd5.json", url="https://sub.example/a/", final_url="https://sub.example/b/")
        t = self.target()
        # request keyed by the redirect target
        # (a positive case that already passed before the union fix; it guards against regressing it)
        t.execute("INSERT INTO requests (type,status,url,host,normalized_key) VALUES ('add','rejected','https://qux.example/ja/','qux.example','qux.example/ja')")
        # path block on the registered path while final_url moved elsewhere
        t.execute("INSERT INTO blocked_hosts (host,path_prefix,reason) VALUES ('sub.example','/a','removal_request')")
        t.executescript(self.export())
        self.assertIsNone(self.listed(t, "qux.example"))
        self.assertIsNone(self.listed(t, "sub.example"))
        keys = export_approved_sql.url_keys(["https://x.example/ja/", "https://x.example/", "https://x.example"])
        self.assertEqual(keys, ["x.example", "x.example/ja"])

    def test_only_candidate_state_hosts_are_exported(self):
        for st in ("rejected", "blocked_by_robots", "deferred"):
            self.db.execute("UPDATE frontier SET state=? WHERE key='gamma.example'", (st,))
            self.db.commit()
            self.assertNotIn("-- gamma.example", self.export(), st)
        self.db.execute("UPDATE frontier SET state='candidate' WHERE key='gamma.example'")
        self.db.commit()
        self.assertIn("-- gamma.example", self.export())

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, "node_modules", ".bin", "wrangler")) and not os.environ.get("MISE_SKIP_WRANGLER"),
                         "wrangler not installed (or MISE_SKIP_WRANGLER)")
    def test_sql_with_semicolons_newlines_and_comment_markers_applies_with_wrangler_local(self):
        nasty = "a; b\n-- c; DROP TABLE sites; it's /* x */ ok"
        self.approve_new("nasty.example", out_pass(description_ja=nasty, description_en=nasty), name="r11.json", title="T; -- x\nsecond")
        state = os.path.join(self.dir, "d1")
        sqlfile = self.path("w.sql")
        run = lambda *a: subprocess.run(["npx", "wrangler", "d1", *a, "--local", "--persist-to", state], cwd=ROOT,
                                        capture_output=True, text=True, timeout=300)
        r = run("migrations", "apply", "mise")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        # alpha is listed first, then re-approved with other tags and categories: the file mixes listed and new sites
        write(sqlfile, self.export())
        r = run("execute", "mise", "--file", sqlfile)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-800:])
        self.imp(self.write_pipeline({"alpha.example": out_pass(categories=["arts", "photography", "architecture"],
                                                                tags=["a", "b", "c", "d", "e"])}, name="rr-w.json", model="sonnet"))
        self.assertEqual(review_state.set_state(self.db, ["alpha.example"], "approved")[0][1], True)
        self.approve_new("zeta.example", out_pass(), name="r12.json")
        sql = self.export()
        self.assertIn("-- alpha.example", sql)
        write(sqlfile, sql)
        for _ in range(2):  # second run: nothing is added
            r = run("execute", "mise", "--file", sqlfile)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-800:])
        q = run("execute", "mise", "--json", "--command",
                "SELECT count(*) AS n, min(description_ja) AS d FROM sites WHERE id LIKE 'fr-nasty%'")
        self.assertEqual(q.returncode, 0, q.stderr[-500:])
        res = json.loads(q.stdout[q.stdout.index("["):])[0]["results"][0]
        self.assertEqual(res["n"], 1)
        self.assertEqual(res["d"], nasty)
        c = run("execute", "mise", "--json", "--command",
                "SELECT (SELECT count(*) FROM site_tags WHERE site_id LIKE 'fr-%') AS t, (SELECT count(*) FROM site_categories WHERE site_id LIKE 'fr-%') AS c")
        got = json.loads(c.stdout[c.stdout.index("["):])[0]["results"][0]
        # alpha 2 (the re-approved 5 tags / 3 categories are skipped), adultsite 2, o'brien 1, gamma 2, nasty 2, zeta 2 tags; 1 category each
        self.assertEqual((got["t"], got["c"]), (11, 6))
        a = run("execute", "mise", "--json", "--command",
                "SELECT (SELECT count(*) FROM sites WHERE id LIKE 'fr-alpha%') AS s, (SELECT count(*) FROM site_tags WHERE site_id LIKE 'fr-alpha%' AND label_en IN ('a','b','c','d','e')) AS t, "
                "(SELECT count(*) FROM site_categories WHERE site_id LIKE 'fr-alpha%' AND category_id LIKE 'arts%') AS c")
        got = json.loads(a.stdout[a.stdout.index("["):])[0]["results"][0]
        self.assertEqual((got["s"], got["t"], got["c"]), (1, 0, 0))

    def test_missing_ledger_or_table(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(export_approved_sql.main(["--db", self.path("none.sqlite"), "--out", self.sqlpath]), 1)
            c = sqlite3.connect(self.path("bare.sqlite")); c.execute("CREATE TABLE frontier (key TEXT)"); c.close()
            self.assertEqual(export_approved_sql.main(["--db", self.path("bare.sqlite"), "--out", self.sqlpath]), 1)


if __name__ == "__main__":
    unittest.main()
