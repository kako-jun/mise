"""Key-less unit tests for scripts/llm (no network, no API key). Run: uv run python3 -m unittest discover tests"""
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
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "llm"))

import export_candidates  # noqa: E402
import export_sites  # noqa: E402
import fetch_about  # noqa: E402
import judge  # noqa: E402

CATS = judge.load_categories()
IDS = set(judge.category_ids(CATS))
SOURCE = "<candidate>\nurl: https://example.org/\ntitle: Example\nmeta_description: 東京都に事務所のある小さな団体です。\n</candidate>"


def good():
    return {
        "description_ja": "小さな団体のサイト。", "description_en": "The site of a small organization.",
        "long_description_ja": None, "long_description_en": None, "long_description_evidence": [],
        "spam": {"verdict": "pass", "kind": "none", "reason": "Plain organization page."},
        "categories": ["business-organizations"], "tags": ["organization"],
        "adult": {"value": "no", "evidence_quote": None},
        "site_scope": "standalone", "site_kind": "organization", "site_kind_reason": "Says it is an organization.",
        "confidence": "medium",
        "location": {"country_code": "JP", "region_code": "JP-13", "evidence_quote": "東京都に事務所のある"},
        "notes": "",
    }


class SchemaTests(unittest.TestCase):
    def test_category_enum_comes_from_migration(self):
        schema = judge.build_schema(CATS)
        enum = schema["properties"]["categories"]["items"]["enum"]
        self.assertEqual(set(enum), IDS)
        self.assertIn("science-astronomy", IDS)

    def test_objects_are_closed_and_required_complete(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(judge.build_schema(CATS))

    def test_no_unsupported_keywords(self):
        blob = json.dumps(judge.build_schema(CATS))
        for bad in ("minLength", "maxLength", "minimum", "maximum", "maxItems"):
            self.assertNotIn(bad, blob)

    def test_system_prompt_is_stable_and_lists_ids(self):
        self.assertEqual(judge.build_system(CATS), judge.build_system(CATS))
        self.assertIn("science-astronomy", judge.build_system(CATS))


class ValidateTests(unittest.TestCase):
    def check(self, mutate):
        o = good()
        mutate(o)
        return judge.validate_output(o, IDS, SOURCE)

    def test_good(self):
        self.assertEqual(judge.validate_output(good(), IDS, SOURCE), [])

    def test_unknown_category(self):
        self.assertTrue(self.check(lambda o: o.update(categories=["nope"])))

    def test_location_quote_must_be_verbatim(self):
        self.assertTrue(self.check(lambda o: o["location"].update(evidence_quote="大阪府に事務所")))

    def test_location_needs_quote_and_prefix(self):
        self.assertTrue(self.check(lambda o: o["location"].update(evidence_quote=None)))
        self.assertTrue(self.check(lambda o: o["location"].update(region_code="US-CA")))

    def test_null_location_ok(self):
        o = good()
        o["location"] = {"country_code": None, "region_code": None, "evidence_quote": None}
        self.assertEqual(judge.validate_output(o, IDS, SOURCE), [])

    def test_descriptions_pair(self):
        self.assertTrue(self.check(lambda o: o.update(description_en=None)))

    def test_deferred_only_for_user_page(self):
        self.assertTrue(self.check(lambda o: o["spam"].update(verdict="deferred")))
        def user_page(o):
            o["site_scope"] = "user_page"
            o["spam"]["verdict"] = "deferred"
        self.assertEqual(self.check(user_page), [])
        self.assertTrue(self.check(lambda o: o.update(site_scope="user_page")))

    def test_adult_yes_needs_quote(self):
        self.assertTrue(self.check(lambda o: o.update(adult={"value": "yes", "evidence_quote": None})))
        self.assertTrue(self.check(lambda o: o.update(adult={"value": "no", "evidence_quote": "x"})))

    def test_long_description_needs_verbatim_evidence(self):
        def ok(o):
            o.update(long_description_ja="東京都にある団体。", long_description_en="An organization in Tokyo.",
                     long_description_evidence=["東京都に事務所のある小さな団体"])
        self.assertEqual(self.check(ok), [])
        def bad(o):
            ok(o)
            o["long_description_evidence"] = ["創業百年の老舗"]
        self.assertTrue(self.check(bad))
        self.assertTrue(self.check(lambda o: o.update(long_description_ja="x", long_description_en="y")))

    def test_extra_or_missing_keys(self):
        o = good()
        del o["notes"]
        self.assertTrue(judge.validate_output(o, IDS, SOURCE))

    def test_parse_response_text(self):
        msg = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(good())}]}
        self.assertEqual(judge.parse_response_text(msg)[1], None)
        self.assertIsNone(judge.parse_response_text(dict(msg, stop_reason="max_tokens"))[0])
        self.assertIsNone(judge.parse_response_text({"stop_reason": "end_turn", "content": [{"type": "text", "text": "{"}]})[0])


class CostAndCacheTests(unittest.TestCase):
    def test_usage_cost(self):
        u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(judge.usage_cost("claude-haiku-5-5", u), 0.10 + 0.50)
        self.assertAlmostEqual(judge.usage_cost("claude-sonnet-5-5", u, batch=True), (2 + 10) * 0.5)
        u = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000}
        self.assertAlmostEqual(judge.usage_cost("claude-sonnet-5-5", u), 2.50 + 0.10)
        u["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000}
        self.assertAlmostEqual(judge.usage_cost("claude-sonnet-5-5", u), 4.00 + 0.10)

    def test_hash_depends_on_everything(self):
        h = judge.input_hash("m", "sys", {"a": 1}, "user", 100, None)
        self.assertEqual(h, judge.input_hash("m", "sys", {"a": 1}, "user", 100, None))
        for args in (("m2", "sys", {"a": 1}, "user", 100, None), ("m", "sys2", {"a": 1}, "user", 100, None),
                     ("m", "sys", {"a": 2}, "user", 100, None), ("m", "sys", {"a": 1}, "user2", 100, None),
                     ("m", "sys", {"a": 1}, "user", 101, None), ("m", "sys", {"a": 1}, "user", 100, "low")):
            self.assertNotEqual(h, judge.input_hash(*args))

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            c = judge.Cache(os.path.join(d, "cache"))
            self.assertIsNone(c.get("k"))
            c.put("k", {"x": 1})
            self.assertEqual(c.get("k"), {"x": 1})

    def test_estimate_tokens_is_conservative(self):
        self.assertGreaterEqual(judge.estimate_tokens("あ" * 100), 100)
        self.assertGreater(judge.estimate_tokens("a" * 300), 90)


class BudgetTests(unittest.TestCase):
    def test_run_budget_stops(self):
        with tempfile.TemporaryDirectory() as d:
            led = judge.Ledger(os.path.join(d, "l.jsonl"), 1.0, 90.0)
            led.check(0.5)
            led.record_call("r", "a", "claude-haiku-5-5", "single", {"input_tokens": 1, "output_tokens": 1}, 0.8, 1.0)
            with self.assertRaises(judge.BudgetExceeded):
                led.check(0.3)

    def test_total_budget_counts_previous_runs_and_reservations(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "l.jsonl")
            first = judge.Ledger(path, 100.0, 1.0)
            first.record_call("r1", "a", "claude-haiku-5-5", "single", {}, 0.6, 1.0)
            second = judge.Ledger(path, 100.0, 1.0)
            self.assertAlmostEqual(second.total(), 0.6)
            with self.assertRaises(judge.BudgetExceeded):
                second.check(0.5)
            second.append({"kind": "reserve", "batch": "b", "usd": 0.3})
            with self.assertRaises(judge.BudgetExceeded):
                second.check(0.2)
            second.append({"kind": "release", "batch": "b", "usd": 0.3})
            second.check(0.2)

    def test_ledger_records_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            led = judge.Ledger(os.path.join(d, "l.jsonl"), 1.0, 90.0)
            led.record_call("r", "a", "claude-haiku-5-5", "single",
                            {"input_tokens": 5, "output_tokens": 6, "cache_creation_input_tokens": 7,
                             "cache_read_input_tokens": 8}, 0.01, 2.0)
            row = json.loads(open(led.path).read())
            self.assertEqual((row["input_tokens"], row["output_tokens"], row["cache_creation_input_tokens"],
                              row["cache_read_input_tokens"]), (5, 6, 7, 8))


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_api(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.jsonl")
            with open(inp, "w") as f:
                f.write(json.dumps({"id": "a", "url": "https://example.org/", "title": "T", "meta_description": "m"}) + "\n")
            calls = []
            orig = judge.http
            judge.http = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network"))
            os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = judge.main([inp, "--model", "haiku", "--model", "sonnet", "--dry-run", "--var-dir", d])
            finally:
                judge.http = orig
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])
            self.assertIn("expected $", buf.getvalue())

    def test_candidate_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jsonl")
            open(p, "w").write('{"url": "https://e.org/"}\n')
            with self.assertRaises(SystemExit):
                judge.read_candidates(p)

    def test_batch_custom_id_sanitised(self):
        self.assertRegex(judge.custom_id_for("a.b/c d"), r"^[A-Za-z0-9_-]{1,64}$")


class AdapterTests(unittest.TestCase):
    def test_export_sites_hides_human_text_by_default(self):
        cands, refs = export_sites.export()
        self.assertEqual(len(cands), len(refs))
        self.assertTrue(all("description_ja" not in c for c in cands))
        c0 = {c["id"]: c for c in cands}["sample-1"]
        self.assertIsNone(c0["meta_description"])
        cands2, _ = export_sites.export(with_human_description=True)
        self.assertTrue({c["id"]: c for c in cands2}["sample-1"]["meta_description"])

    def test_export_candidates_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "f.sqlite")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE frontier (key TEXT, url TEXT, final_url TEXT, state TEXT, title TEXT, description TEXT,"
                        " lang TEXT, kana_count INT, text_len INT, ad_hits INT, ext_refs INT, flags TEXT, text_head TEXT,"
                        " last_http_status INT, robots_status TEXT, personal_score INT)")
            con.execute("INSERT INTO frontier VALUES ('a.example','https://a.example/',NULL,'candidate','T','D','ja',1,2,0,0,"
                        "'{\"noindex\": false}','head',200,'allowed',1)")
            con.execute("INSERT INTO frontier VALUES ('b.example','https://b.example/',NULL,'rejected','T','D','ja',1,2,0,0,NULL,'',200,'allowed',1)")
            con.commit()
            con.close()
            rows = export_candidates.export(db)
            self.assertEqual([r["id"] for r in rows], ["a_example"])
            self.assertNotIn("personal_score", json.dumps(rows))
            con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
            self.assertEqual(con.execute("select count(*) from frontier").fetchone()[0], 2)

    def test_about_links(self):
        links = [("/profile.html", "プロフィール"), ("/blog/1", "最新記事の紹介"), ("https://other.example/about", "About"),
                 ("/about/", "About"), ("/x.pdf", "about"), ("/", "home")]
        got = fetch_about.about_links("https://s.example/", links)
        self.assertEqual(sorted(got), ["https://s.example/about/", "https://s.example/profile.html"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- pipeline.py
import pipeline  # noqa: E402


def tri(**kw):
    t = {"i": 1, "v": "p", "kd": "-", "sk": "i", "ad": "n", "aq": None, "sc": "s", "cf": "m", "cat": ["science-astronomy"], "r": ""}
    t.update(kw)
    return t


def des(**kw):
    d = {"i": 1, "dj": "星の写真のサイト。", "de": "A site of star photographs.", "lj": None, "le": None, "ev": [],
         "tg": ["astronomy"], "cc": None, "rc": None, "q": None}
    d.update(kw)
    return d


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.args = type("A", (), dict(body_limit=9000, effort=None, thinking_off=False, cache_ttl="5m", group=5,
                                       cache_dir="/nonexistent/c", ledger="/nonexistent/l", budget_run=1.0,
                                       budget_total=90.0, mode="single"))()
        self.p = pipeline.Pipeline(self.args, CATS)

    def test_wrapper_schemas_closed_and_codes_in_enums(self):
        for stage in ("triage", "describe", "full"):
            blob = json.dumps(self.p.schemas[stage])
            for bad in ("minLength", "maxLength", "maxItems", "minimum"):
                self.assertNotIn(bad, blob)
            self.assertIs(self.p.schemas[stage]["additionalProperties"], False)
        item = self.p.schemas["triage"]["properties"]["r"]["items"]
        self.assertEqual(set(item["properties"]["v"]["enum"]), set("psrd"))
        self.assertEqual(set(item["properties"]["cat"]["items"]["enum"]), IDS)

    def test_triage_item_roundtrip_to_full_shape(self):
        full, problem = pipeline.check_item("triage", tri(), 1, IDS, SOURCE)
        self.assertIsNone(problem)
        self.assertEqual(full["spam"]["verdict"], "pass")
        self.assertEqual(full["site_kind"], "individual")

    def test_triage_rejects_wrong_index_unknown_category_and_bad_deferred(self):
        self.assertIsNotNone(pipeline.check_item("triage", tri(i=2), 1, IDS, SOURCE)[1])
        self.assertIsNotNone(pipeline.check_item("triage", tri(cat=["nope"]), 1, IDS, SOURCE)[1])
        self.assertIsNotNone(pipeline.check_item("triage", tri(v="d"), 1, IDS, SOURCE)[1])
        self.assertIsNone(pipeline.check_item("triage", tri(v="d", sc="u"), 1, IDS, SOURCE)[1])
        self.assertIsNotNone(pipeline.check_item("triage", tri(ad="y"), 1, IDS, SOURCE)[1])  # yes needs a quote

    def test_describe_validates_quotes_against_own_source(self):
        base, _ = pipeline.check_item("triage", tri(), 1, IDS, SOURCE)
        ok, problem = pipeline.check_item("describe", des(lj="東京都の団体。", le="An organization in Tokyo.",
                                                          ev=["東京都に事務所のある小さな団体"]), 1, IDS, SOURCE, base)
        self.assertIsNone(problem)
        self.assertEqual(ok["long_description_ja"], "東京都の団体。")
        _, problem = pipeline.check_item("describe", des(lj="x", le="y", ev=["not in the text"]), 1, IDS, SOURCE, base)
        self.assertIn("verbatim", problem)

    def test_group_text_numbers_candidates_and_key_ignores_group_position(self):
        a = {"id": "a", "url": "https://a.example/", "title": "A"}
        b = {"id": "b", "url": "https://b.example/", "title": "B"}
        text = self.p.group_text([(a, None), (b, None)])
        self.assertIn('<candidate i="1">', text)
        self.assertIn('<candidate i="2">', text)
        self.assertEqual(self.p.key("triage", "m", a), self.p.key("triage", "m", a))
        self.assertNotEqual(self.p.key("triage", "m", a), self.p.key("describe", "m", a))
        self.assertNotEqual(self.p.key("triage", "m", a), self.p.key("triage", "m", b))

    def test_request_shape_and_thinking(self):
        a = {"id": "a", "url": "https://a.example/", "title": "A"}
        req = self.p.request("triage", "claude-haiku-5-5", [(a, None)])
        self.assertEqual(req["system"][0]["cache_control"]["type"], "ephemeral")
        self.assertEqual(req["output_config"]["effort"], "low")
        self.assertNotIn("thinking", req)
        self.args.thinking_off = True
        self.assertEqual(self.p.request("triage", "claude-haiku-5-5", [(a, None)])["thinking"], {"type": "disabled"})
        self.assertEqual(self.p.request("triage", "claude-sonnet-5-5", [(a, None)])["thinking"], {"type": "between_tools"})

    def test_process_only_flags_bad_items(self):
        a = {"id": "a", "url": "https://a.example/", "title": "A"}
        b = {"id": "b", "url": "https://b.example/", "title": "B"}
        self.p.ledger = judge.Ledger(os.path.join(tempfile.mkdtemp(), "l.jsonl"), 1.0, 90.0)
        msg = {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
               "content": [{"type": "text", "text": json.dumps({"r": [tri(i=1), tri(i=2, cat=["nope"])]})}]}
        res = self.p.process("triage", "claude-haiku-5-5", [(a, None, None), (b, None, None)], msg, 1.0)
        self.assertIsNotNone(res[0][0])
        self.assertIsNone(res[1][0])
        self.assertEqual(self.p.stats["triage"]["items"], 2)
        msg["content"][0]["text"] = json.dumps({"r": [tri(i=1)]})
        res = self.p.process("triage", "claude-haiku-5-5", [(a, None, None), (b, None, None)], msg, 1.0)
        self.assertEqual(res[1][1], "missing from response")

    def test_dry_run_offline(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.jsonl")
            with open(inp, "w") as f:
                for n in range(7):
                    f.write(json.dumps({"id": f"c{n}", "url": f"https://e{n}.org/", "title": "T", "meta_description": "m"}) + "\n")
            orig = judge.http
            judge.http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = pipeline.main([inp, "--stages", "two", "--group", "3", "--dry-run", "--var-dir", d])
            finally:
                judge.http = orig
            self.assertEqual(rc, 0)
            self.assertIn("triage: 3 requests", buf.getvalue())
