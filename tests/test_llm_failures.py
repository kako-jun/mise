"""Failure-path tests for scripts/llm with the HTTP layer mocked (no network, no API key, no money).

Covers: max_tokens truncation -> doubled retry (and the budget check seeing the doubled value), retry limits,
retrying only the invalid items, batch reservation / release, ledger missing / corrupt / locking, exact budget
boundary, escalation failure, non-idempotent POST handling, cache-write and long-tier pricing.
"""
import argparse
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "llm"))

import fetch_about  # noqa: E402
import judge  # noqa: E402
import ledger_release  # noqa: E402
import merge_ledgers  # noqa: E402
import pipeline  # noqa: E402
from test_llm_judge import CATS, IDS, SOURCE, des, good, tri  # noqa: E402

HAIKU = "claude-haiku-5-5"
CAND = {"id": "a", "url": "https://a.example/", "title": "A", "meta_description": "東京都に事務所のある小さな団体です。"}
CAND2 = {"id": "b", "url": "https://b.example/", "title": "B", "meta_description": "B site"}


def msg(payload, stop="end_turn", usage=None):
    return {"stop_reason": stop, "usage": usage or {"input_tokens": 1000, "output_tokens": 100},
            "content": [{"type": "text", "text": json.dumps(payload)}]}


class Fake:
    """Replaces judge.http / pipeline.http: pops queued replies and records every request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, body=None, raw=False, retries=4, idempotent=None):
        self.requests.append((method, url, body))
        r = self.replies.pop(0)
        if callable(r):
            r = r(method, url, body)
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.args = argparse.Namespace(
            body_limit=9000, effort=None, thinking_off=False, cache_ttl="5m", group=1, mode="single",
            cache_dir=os.path.join(self.tmp.name, "cache"), ledger=self.ledger_path, budget_run=5.0,
            budget_total=90.0, poll_seconds=0, var_dir=self.tmp.name, max_tokens=3000,
            escalate_model=None)

    def patch_http(self, fake):
        for mod in (judge, pipeline):
            p = mock.patch.object(mod, "http", fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def rows(self):
        with open(self.ledger_path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]


class PipelineFailurePaths(Base):
    def pipe(self):
        return pipeline.Pipeline(self.args, CATS)

    def test_truncation_retries_with_doubled_max_tokens_and_budget_sees_it(self):
        fake = Fake([msg({"r": []}, stop="max_tokens"), msg({"r": [tri()]})])
        self.patch_http(fake)
        p = self.pipe()
        done = {}
        p._run_chunk("triage", HAIKU, [(CAND, None, None)], done)
        self.assertIn("a", done)
        first, second = (r[2]["max_tokens"] for r in fake.requests)
        self.assertEqual(first, 160 + 400)
        self.assertEqual(second, 2 * (160 + 400))
        reserves = [r["usd"] for r in self.rows() if r["kind"] == "reserve"]
        self.assertGreater(reserves[1], reserves[0])  # the check used the doubled max_tokens, not the default
        w1 = p.worst("triage", HAIKU, [(CAND, None)])[0]
        w2 = p.worst("triage", HAIKU, [(CAND, None)], max_tokens=second)[0]
        self.assertAlmostEqual(reserves[0], round(w1, 6))
        self.assertAlmostEqual(reserves[1], round(w2, 6))
        self.assertEqual(sum(1 for r in self.rows() if r["kind"] == "release"), 2)  # nothing left reserved

    def test_retry_limit_is_one_and_failure_is_reported(self):
        fake = Fake([msg({"r": []}, stop="max_tokens"), msg({"r": []}, stop="max_tokens")])
        self.patch_http(fake)
        p = self.pipe()
        p._run_chunk("triage", HAIKU, [(CAND, None, None)], {})
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("a", p.failures)
        self.assertIn("truncated", p.failures["a"])

    def test_only_invalid_items_are_retried(self):
        bad = tri(i=2, cat=["nope"])
        fake = Fake([msg({"r": [tri(i=1), bad]}), msg({"r": [tri(i=1)]})])
        self.patch_http(fake)
        p = self.pipe()
        done = {}
        p._run_chunk("triage", HAIKU, [(CAND, None, None), (CAND2, None, None)], done)
        self.assertEqual(set(done), {"a", "b"})
        self.assertEqual(len(fake.requests), 2)
        retry_text = fake.requests[1][2]["messages"][0]["content"]
        self.assertIn("B site", retry_text)
        self.assertNotIn("東京都", retry_text)  # candidate a was valid and is not sent again
        self.assertEqual(p.stats["triage"]["retry_items"], 1)

    def test_budget_stops_before_the_call(self):
        self.args.budget_run = 0.0000001
        fake = Fake([])
        self.patch_http(fake)
        with self.assertRaises(judge.BudgetExceeded):
            self.pipe()._run_chunk("triage", HAIKU, [(CAND, None, None)], {})
        self.assertEqual(fake.requests, [])

    def test_failed_request_releases_the_reservation(self):
        def boom(*a):
            raise SystemExit("API error 400")
        self.patch_http(Fake([boom]))
        with self.assertRaises(SystemExit):
            self.pipe()._run_chunk("triage", HAIKU, [(CAND, None, None)], {})
        self.assertAlmostEqual(judge.Ledger(self.ledger_path, 1, 1).total(), 0.0)

    def test_ambiguous_post_keeps_the_reservation(self):
        def lost(*a):
            raise judge.AmbiguousRequest("network error")
        self.patch_http(Fake([lost]))
        with self.assertRaises(judge.AmbiguousRequest):
            self.pipe()._run_chunk("triage", HAIKU, [(CAND, None, None)], {})
        self.assertGreater(judge.Ledger(self.ledger_path, 1, 1).total(), 0.0)  # may have been billed

    def batch_replies(self, results_lines):
        def status(m, u, b):
            return {"processing_status": "ended", "results_url": "https://x/results"}
        return [{"id": "msgbatch_1"}, status, "\n".join(json.dumps(x) for x in results_lines)]

    def test_batch_reserves_then_releases_and_records_unmatched_rows(self):
        self.args.mode = "batch"
        ok = {"custom_id": "triage-0", "result": {"type": "succeeded", "message": msg({"r": [tri()]})}}
        stray = {"custom_id": "triage-99", "result": {"type": "succeeded", "message": msg({"r": []})}}
        weird = {"custom_id": "nonsense", "result": {"type": "succeeded", "message": msg({"r": []})}}
        fake = Fake(self.batch_replies([ok, stray, weird]))
        self.patch_http(fake)
        p = self.pipe()
        done = p.run_stage("triage", HAIKU, [(CAND, None, None)])
        self.assertIn("a", done)
        kinds = [r["kind"] for r in self.rows()]
        self.assertEqual(kinds[0], "reserve")
        self.assertEqual(kinds[-1], "release")
        self.assertEqual(kinds.count("call"), 3)  # the matched row and both unmatched (billed) rows
        notes = [r["note"] for r in self.rows() if r["kind"] == "call"]
        self.assertEqual(notes.count("unmatched custom_id"), 2)
        reserve = next(r for r in self.rows() if r["kind"] == "reserve")
        release = next(r for r in self.rows() if r["kind"] == "release")
        self.assertEqual((reserve["batch"], reserve["usd"]), (release["batch"], release["usd"]))
        self.assertLess(judge.Ledger(self.ledger_path, 1, 1).total(), reserve["usd"])  # actual cost replaces the reserve
        self.assertAlmostEqual(p.ledger.run_reserved, 0.0)

    def test_batch_rejected_submit_releases(self):
        self.args.mode = "batch"

        def reject(*a):
            raise SystemExit("API error 400")
        self.patch_http(Fake([reject]))
        with self.assertRaises(SystemExit):
            self.pipe().run_stage("triage", HAIKU, [(CAND, None, None)])
        self.assertAlmostEqual(judge.Ledger(self.ledger_path, 1, 1).total(), 0.0)

    def test_args_thinking_off_with_high_effort_is_rejected_early(self):
        ap = argparse.ArgumentParser()
        a = argparse.Namespace(thinking_off=True, effort="xhigh", group=5)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.check_args(ap, a)
        pipeline.check_args(ap, argparse.Namespace(thinking_off=True, effort="low", group=5))

    def test_triage_aq_without_adult_yes_is_an_error(self):
        full, problem = pipeline.check_item("triage", tri(aq="東京都"), 1, IDS, SOURCE)
        self.assertIsNone(full)
        self.assertIn("aq", problem)


class LedgerTests(Base):
    def test_budget_boundary_is_inclusive(self):
        led = judge.Ledger(self.ledger_path, 1.0, 2.0)
        led.check(1.0)  # exactly the run budget is allowed
        with self.assertRaises(judge.BudgetExceeded):
            led.check(1.0001)
        led.record_call("r", "a", HAIKU, "single", {}, 0.5, 1.0)
        led.check(0.5)
        with self.assertRaises(judge.BudgetExceeded):
            led.check(0.5001)
        other = judge.Ledger(self.ledger_path, 100.0, 2.0)  # total budget: 0.5 spent
        other.check(1.5)
        with self.assertRaises(judge.BudgetExceeded):
            other.check(1.5001)

    def test_parallel_reservations_cannot_both_pass(self):
        results = []
        barrier = threading.Barrier(2)

        def worker(n):
            led = judge.Ledger(self.ledger_path, 100.0, 1.0)  # separate objects = separate processes' view
            barrier.wait()
            try:
                led.reserve(0.6, f"t{n}")
                results.append("ok")
            except judge.BudgetExceeded:
                results.append("stopped")

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["ok", "stopped"])

    def test_missing_ledger_with_existing_cache_stops_unless_initialised(self):
        cache = os.path.join(self.tmp.name, "llm-cache")
        os.makedirs(cache)
        with open(os.path.join(cache, "k.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        led = judge.Ledger(self.ledger_path, 1.0, 1.0)
        with self.assertRaises(judge.LedgerError):
            led.ensure_ready(cache)
        self.assertFalse(os.path.exists(self.ledger_path))
        led.ensure_ready(cache, init=True)
        self.assertTrue(os.path.exists(self.ledger_path))
        judge.Ledger(self.ledger_path, 1.0, 1.0).ensure_ready(cache)  # now fine without the flag

    def test_missing_ledger_without_results_is_created(self):
        judge.Ledger(self.ledger_path, 1.0, 1.0).ensure_ready(os.path.join(self.tmp.name, "llm-cache"))
        self.assertTrue(os.path.exists(self.ledger_path))

    def test_corrupt_ledger_stops(self):
        for bad in ('{"kind": "call", "cost_usd": 0.1}\n{"kind": "ca', '{"kind": "call"}\n', "[1]\n",
                    '{"kind": "mystery", "usd": 1}\n'):
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                f.write(bad)
            with self.assertRaises(judge.LedgerError):
                judge.Ledger(self.ledger_path, 1.0, 1.0).total()
            with self.assertRaises(judge.LedgerError):
                judge.Ledger(self.ledger_path, 1.0, 1.0).check(0.01)

    def test_merge_is_idempotent_and_sums(self):
        a = os.path.join(self.tmp.name, "a.jsonl")
        b = os.path.join(self.tmp.name, "b.jsonl")
        judge.Ledger(a, 9, 9).record_call("r", "x", HAIKU, "single", {}, 0.25, 1)
        judge.Ledger(b, 9, 9).record_call("r", "y", HAIKU, "single", {}, 0.5, 1)
        with contextlib.redirect_stderr(io.StringIO()):
            merge_ledgers.main([a, b, "--into", self.ledger_path])
            merge_ledgers.main([a, b, "--into", self.ledger_path])  # second run adds nothing
        self.assertAlmostEqual(judge.Ledger(self.ledger_path, 9, 9).total(), 0.75)
        self.assertEqual(len(self.rows()), 2)


class BatchResume(Base):
    """--batch-resume must be safe to repeat: the ledger total neither shrinks nor grows after the first settle."""

    def judge_with_batch(self):
        a = JudgeFailurePaths.judge_obj(self)
        return a

    def ended(self):
        def status(m, u, b):
            return {"processing_status": "ended", "results_url": "https://x/r"}
        return status

    def rows_text(self):
        ok = {"custom_id": "a", "result": {"type": "succeeded", "message": msg(good())}}
        return json.dumps(ok)

    def total(self):
        return judge.Ledger(self.ledger_path, 9, 9).total()

    def test_resume_of_a_settled_batch_is_refused_and_changes_nothing(self):
        j = self.judge_with_batch()
        self.patch_http(Fake([{"id": "msgbatch_5"}, self.ended(), self.rows_text()]))
        with contextlib.redirect_stderr(io.StringIO()):
            j.run_batch(HAIKU, [CAND])
        after_first = self.total()
        self.assertGreater(after_first, 0)
        n_rows = len(self.rows())
        for _ in range(2):
            self.patch_http(Fake([self.ended(), self.rows_text()]))
            j2 = self.judge_with_batch()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                j2.run_batch(HAIKU, [CAND], resume="msgbatch_5")
            self.assertIn("already settled", str(cm.exception))
            self.assertAlmostEqual(self.total(), after_first)
            self.assertEqual(len(self.rows()), n_rows)

    def test_replaying_an_unsettled_batch_does_not_double_record(self):
        """Crash after the results were recorded but before the settled mark: resume twice, total stays put."""
        j = self.judge_with_batch()
        self.patch_http(Fake([{"id": "msgbatch_6"}, self.ended(), self.rows_text()]))
        with contextlib.redirect_stderr(io.StringIO()):
            j.run_batch(HAIKU, [CAND])
        settled = self.total()
        state = os.path.join(self.tmp.name, "llm-batches", "msgbatch_6.json")
        st = judge.read_state(state)
        self.assertTrue(st["settled"])
        for _ in range(2):
            judge.write_state(state, dict(st, settled=False))  # simulate the crash
            self.patch_http(Fake([self.ended(), self.rows_text()]))
            with contextlib.redirect_stderr(io.StringIO()):
                self.judge_with_batch().run_batch(HAIKU, [CAND], resume="msgbatch_6")
            self.assertAlmostEqual(self.total(), settled)
        self.assertEqual(sum(1 for r in self.rows() if r["kind"] == "call"), 1)
        self.assertEqual(sum(1 for r in self.rows() if r["kind"] == "release"), 1)

    def test_release_is_idempotent(self):
        led = judge.Ledger(self.ledger_path, 9, 9)
        led.reserve(0.5, "t1")
        led.release("t1")
        led.release("t1")
        led.release("t1", 0.5)
        self.assertAlmostEqual(led.total(), 0.0)

    def test_pipeline_batch_resumes_after_a_failed_poll_without_resubmitting(self):
        self.args.mode = "batch"
        ok = {"custom_id": "triage-0", "result": {"type": "succeeded", "message": msg({"r": [tri()]})}}

        def poll_dies(m, u, b):
            raise SystemExit("network error on GET")
        fake1 = Fake([{"id": "msgbatch_7"}, poll_dies])
        self.patch_http(fake1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        reserved = self.total()
        self.assertGreater(reserved, 0)  # still reserved, not lost
        fake2 = Fake([self.ended(), json.dumps(ok)])
        self.patch_http(fake2)
        with contextlib.redirect_stderr(io.StringIO()):
            done = pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        self.assertIn("a", done)
        self.assertTrue(all(r[0] == "GET" for r in fake2.requests))  # no second POST /batches
        self.assertLess(self.total(), reserved)  # actual cost replaced the reservation
        left = ledger_release.open_reservations(judge.Ledger(self.ledger_path, 9, 9))
        self.assertEqual(left, {})

    def test_pipeline_unknown_submit_blocks_resubmission_and_manual_release_works(self):
        self.args.mode = "batch"

        def lost(*a):
            raise judge.AmbiguousRequest("network error")
        self.patch_http(Fake([lost]))
        with self.assertRaises(judge.AmbiguousRequest):
            pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        fake = Fake([])
        self.patch_http(fake)
        with self.assertRaises(SystemExit) as cm:
            pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        self.assertIn("unknown whether a batch exists", str(cm.exception))
        self.assertEqual(fake.requests, [])
        led = judge.Ledger(self.ledger_path, 9, 9)
        tag = next(iter(ledger_release.open_reservations(led)))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ledger_release.main([tag, "--ledger", self.ledger_path]), 0)
        self.assertAlmostEqual(led.total(), 0.0)

    def test_crash_while_processing_results_then_rerun_does_not_pay_again(self):
        """Repro: the first run dies after caching only part of the rows. The unfinished list changed, so a
        state keyed by it would be lost; the re-run must resume the same batch, not submit a new one."""
        self.args.mode = "batch"
        rows = [{"custom_id": f"triage-{k}", "result": {"type": "succeeded", "message": msg({"r": [tri()]})}}
                for k in range(2)]
        a1 = pipeline.Pipeline(self.args, CATS)
        orig = a1._collect
        calls = []

        def dying(*args, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("crash while processing the second row")
            return orig(*args, **kw)
        a1._collect = dying
        self.patch_http(Fake([{"id": "msgbatch_8"}, self.ended(), "\n".join(json.dumps(r) for r in rows)]))
        work = [(CAND, None, None), (CAND2, None, None)]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            a1.run_stage("triage", HAIKU, work)
        calls_after_crash = sum(1 for r in self.rows() if r["kind"] == "call")
        self.assertEqual(calls_after_crash, 1)
        fake2 = Fake([self.ended(), "\n".join(json.dumps(r) for r in rows)])
        self.patch_http(fake2)
        p2 = pipeline.Pipeline(self.args, CATS)
        with contextlib.redirect_stderr(io.StringIO()):
            done = p2.run_stage("triage", HAIKU, work)  # a is cached now; the todo list is just [b]
        self.assertEqual(set(done), {"a", "b"})
        self.assertTrue(all(r[0] == "GET" for r in fake2.requests), "a second batch was submitted")
        self.assertEqual(sum(1 for r in self.rows() if r["kind"] == "call"), 2)  # one per row, never twice
        self.assertEqual(ledger_release.open_reservations(judge.Ledger(self.ledger_path, 9, 9)), {})
        # and once settled, the same command submits fresh work again (nothing left to resume)
        self.assertEqual([st for _, st in judge.unsettled_states(os.path.join(self.tmp.name, "llm-batches"))], [])

    def test_ctrl_c_during_batch_submit_is_treated_as_unknown(self):
        self.args.mode = "batch"

        def interrupted(*a):
            raise KeyboardInterrupt()
        self.patch_http(Fake([interrupted]))
        with self.assertRaises(KeyboardInterrupt):
            pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        led = judge.Ledger(self.ledger_path, 9, 9)
        self.assertGreater(led.total(), 0)  # reservation kept
        (path, st), = judge.unsettled_states(os.path.join(self.tmp.name, "llm-batches"))
        self.assertIsNone(st["batch_id"])
        fake = Fake([])
        self.patch_http(fake)
        with self.assertRaises(SystemExit) as cm:
            pipeline.Pipeline(self.args, CATS).run_stage("triage", HAIKU, [(CAND, None, None)])
        self.assertIn("ledger_release.py", str(cm.exception))
        self.assertEqual(fake.requests, [])  # no second submit

    def test_ctrl_c_during_single_call_keeps_the_reservation(self):
        def interrupted(*a):
            raise KeyboardInterrupt()
        self.patch_http(Fake([interrupted]))
        with self.assertRaises(KeyboardInterrupt):
            pipeline.Pipeline(self.args, CATS)._run_chunk("triage", HAIKU, [(CAND, None, None)], {})
        self.assertGreater(judge.Ledger(self.ledger_path, 9, 9).total(), 0)
        led = judge.Ledger(self.ledger_path, 9, 9)
        with self.assertRaises(KeyboardInterrupt), led.hold(0.1, "h1"):
            raise KeyboardInterrupt()
        self.assertIn("h1", ledger_release.open_reservations(led))

    def test_judge_batch_submit_interrupted_blocks_a_second_submit(self):
        j = self.judge_with_batch()

        def interrupted(*a):
            raise KeyboardInterrupt()
        self.patch_http(Fake([interrupted]))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            j.run_batch(HAIKU, [CAND])
        self.assertGreater(self.total(), 0)
        fake = Fake([])
        self.patch_http(fake)
        with self.assertRaises(SystemExit) as cm:
            self.judge_with_batch().run_batch(HAIKU, [CAND])
        self.assertIn("UNKNOWN", str(cm.exception))
        self.assertEqual(fake.requests, [])

    def test_old_format_state_file_is_not_resumed(self):
        d = os.path.join(self.tmp.name, "llm-batches")
        os.makedirs(d)
        with open(os.path.join(d, "msgbatch_old.json"), "w", encoding="utf-8") as f:
            json.dump({"model": HAIKU, "reserved_usd": 0.1, "ids": ["a"]}, f)
        self.patch_http(Fake([]))
        with self.assertRaises(SystemExit) as cm:
            self.judge_with_batch().run_batch(HAIKU, [CAND], resume="msgbatch_old")
        self.assertIn("old format", str(cm.exception))

    def test_run_ids_differ_between_processes(self):
        with mock.patch("os.getpid", return_value=111):
            a = judge.new_run_id()
        with mock.patch("os.getpid", return_value=222):
            b = judge.new_run_id()
        self.assertNotEqual(a, b)


class JudgeFailurePaths(Base):
    def judge_obj(self):
        a = judge.make_parser().parse_args(["unused.jsonl"])
        for k, v in vars(self.args).items():
            if k in ("escalate_model",):
                setattr(a, k, v)
        a.cache_dir, a.ledger, a.var_dir = self.args.cache_dir, self.ledger_path, self.tmp.name
        a.budget_run, a.budget_total = 5.0, 90.0
        return judge.Judge(a, CATS)

    def test_escalation_failure_keeps_the_valid_first_result(self):
        low = good()
        low["confidence"] = "low"
        broken = msg({"nope": 1})
        fake = Fake([msg(low), broken, broken])
        self.patch_http(fake)
        j = self.judge_obj()
        j.args.escalate_model = "claude-sonnet-5-5"
        out = j.run_single(HAIKU, [CAND])
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(out[0]["output"])  # the haiku result is not replaced by a failed one
        self.assertEqual(out[0]["model"], HAIKU)
        self.assertEqual(out[0]["escalation_failed"]["model"], "claude-sonnet-5-5")
        self.assertEqual(len(fake.requests), 3)

    def test_single_mode_reserves_before_the_call(self):
        seen = []

        def reply(m, u, b):
            seen.append(judge.Ledger(self.ledger_path, 1, 1).total())  # what a parallel run would see
            return msg(good())
        self.patch_http(Fake([reply]))
        self.judge_obj().judge_one(HAIKU, CAND)
        self.assertGreater(seen[0], 0.0)

    def test_judge_batch_unmatched_row_is_billed_and_reservation_released(self):
        a = self.judge_obj()
        a.args.max_tokens = 3000
        ok_row = {"custom_id": "a", "result": {"type": "succeeded", "message": msg(good())}}
        stray = {"custom_id": "zzz", "result": {"type": "succeeded", "message": msg(good())}}

        def status(m, u, b):
            return {"processing_status": "ended", "results_url": "https://x/r"}
        self.patch_http(Fake([{"id": "msgbatch_9"}, status, "\n".join(json.dumps(x) for x in (ok_row, stray))]))
        with contextlib.redirect_stderr(io.StringIO()):
            res = a.run_batch(HAIKU, [CAND])
        self.assertIsNotNone(res[0]["output"])
        calls = [r for r in self.rows() if r["kind"] == "call"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.rows()[-1]["kind"], "release")
        self.assertAlmostEqual(a.ledger.run_reserved, 0.0)


class HttpTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        p = mock.patch("time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_post_is_not_resent_after_timeout(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as uo:
            with self.assertRaises(judge.AmbiguousRequest) as cm:
                judge.http("POST", judge.API, {"x": 1})
        self.assertEqual(uo.call_count, 1)
        self.assertNotIn("test-not-a-real-key", str(cm.exception))

    def test_post_is_not_resent_after_connection_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("reset")) as uo:
            with self.assertRaises(judge.AmbiguousRequest):
                judge.http("POST", judge.API + "/batches", {"x": 1})
        self.assertEqual(uo.call_count, 1)

    def test_get_and_count_tokens_are_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("t")) as uo:
            with self.assertRaises(SystemExit) as cm:
                judge.http("GET", judge.API + "/batches/x", retries=2)
        self.assertEqual(uo.call_count, 3)
        self.assertNotIsInstance(cm.exception, judge.AmbiguousRequest)
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("t")) as uo:
            with self.assertRaises(SystemExit):
                judge.http("POST", judge.API + "/count_tokens", {}, retries=1, idempotent=True)
        self.assertEqual(uo.call_count, 2)

    def test_status_429_is_retried_for_post(self):
        err = urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b"slow down"))
        self.addCleanup(err.close)
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as uo:
            self.assertEqual(judge.http("POST", judge.API, {"x": 1}), {"ok": True})
        self.assertEqual(uo.call_count, 2)


class PricingTests(unittest.TestCase):
    def test_worst_case_includes_cache_write_premium(self):
        sys_text, user = "s" * 30000, "u" * 300
        w5 = judge.worst_case(HAIKU, sys_text, user, 100, cache_ttl="5m")[0]
        w1 = judge.worst_case(HAIKU, sys_text, user, 100, cache_ttl="1h")[0]
        plain = (judge.estimate_tokens(sys_text) * 0.10 + (judge.estimate_tokens(user) + 400) * 0.10 + 100 * 0.50) / 1e6
        self.assertGreater(w5, plain)
        self.assertGreater(w1, w5)
        n = judge.estimate_tokens(sys_text)
        self.assertAlmostEqual(w5 - plain, n * (0.125 - 0.10) / 1e6)
        self.assertAlmostEqual(w1 - plain, n * (0.20 - 0.10) / 1e6)

    def test_haiku_long_prompt_is_refused(self):
        with self.assertRaises(judge.BudgetExceeded):
            judge.worst_case(HAIKU, "s", "あ" * 120_000, 100)
        judge.worst_case("claude-sonnet-5-5", "s", "あ" * 120_000, 100)  # sonnet has no such tier here


class QuoteAndEnumChecks(unittest.TestCase):
    def check(self, mutate, source=SOURCE):
        o = good()
        mutate(o)
        return judge.validate_output(o, IDS, source)

    def test_labels_and_placeholders_are_not_quotable(self):
        def ev(q):
            return lambda o: o.update(adult={"value": "yes", "evidence_quote": q})
        self.assertTrue(self.check(ev("url: https://example.org/")))
        self.assertTrue(self.check(ev("title: Example")))
        self.assertTrue(self.check(ev("(none)")), "placeholder text is scaffolding")
        self.assertEqual(self.check(ev("Example")), [])  # the value itself is fine

    def test_whitespace_is_normalised_but_words_are_not_glued(self):
        src = "<candidate>\nurl: https://e.org/\nbody_excerpt:\nwe run\nthe small shop support .\n東京都に 事務所がある\n</candidate>"

        def loc(q):
            return lambda o: o.update(location={"country_code": "JP", "region_code": "JP-13", "evidence_quote": q})
        self.assertEqual(self.check(loc("we run the small shop support."), src), [])  # "support ." (HTML join) == "support."
        self.assertEqual(self.check(loc("東京都に事務所がある"), src), [])  # no space next to non-ASCII
        self.assertTrue(self.check(loc("we runthe small shop"), src))  # ASCII words are never glued together

    def test_empty_body_placeholder_is_not_quotable(self):
        src = "<candidate>\nurl: https://e.org/\ntitle: T\nmeta_description: (none)\nlang: (none)\nfeatures: (none)\nbody_excerpt:\n(none)\n</candidate>"
        o = good()
        o.update(location={"country_code": None, "region_code": None, "evidence_quote": None})
        for q in ("https://e.org/ T (none)", "(none)", "T (none)"):
            o.update(adult={"value": "yes", "evidence_quote": q})
            self.assertTrue(judge.validate_output(o, IDS, src), q)

    def test_punctuation_space_is_dropped_only_before_a_word_end(self):
        self.assertEqual(judge._norm("foo ."), judge._norm("foo."))
        self.assertEqual(judge._norm("foo , bar"), judge._norm("foo, bar"))
        self.assertNotEqual(judge._norm("foo .bar"), judge._norm("foo.bar"))

    def test_body_line_starting_with_a_label_is_the_sites_own_text(self):
        src = "<candidate>\nurl: https://e.org/\ntitle: T\nbody_excerpt:\ntitle: Our history\n</candidate>"
        o = good()
        o.update(location={"country_code": None, "region_code": None, "evidence_quote": None})
        o.update(adult={"value": "yes", "evidence_quote": "title: Our history"})
        self.assertEqual(judge.validate_output(o, IDS, src), [])
        o.update(adult={"value": "yes", "evidence_quote": "title: T"})  # the header label is scaffolding
        self.assertTrue(judge.validate_output(o, IDS, src))

    def test_verdict_kind_combinations(self):
        def vk(v, k, scope="standalone"):
            return lambda o: (o["spam"].update(verdict=v, kind=k), o.update(site_scope=scope))
        self.assertTrue(self.check(vk("pass", "spam")))
        self.assertTrue(self.check(vk("reject", "none")))
        self.assertTrue(self.check(vk("reject", "other")))
        self.assertTrue(self.check(vk("suspect", "none")))
        self.assertTrue(self.check(vk("pass", "insufficient_input")))
        self.assertTrue(self.check(vk("deferred", "spam", "user_page")))
        self.assertEqual(self.check(vk("reject", "parking")), [])
        self.assertEqual(self.check(vk("suspect", "insufficient_input")), [])
        self.assertEqual(self.check(vk("deferred", "none", "user_page")), [])

    def test_english_long_description_has_a_cap(self):
        def long_en(o):
            o.update(long_description_ja="東京都の団体。", long_description_en="x" * 1601,
                     long_description_evidence=["東京都に事務所のある小さな団体"])
        self.assertTrue(any("long_description_en" in e for e in self.check(long_en)))


class SafeHost(unittest.TestCase):
    def resolve(self, *addrs):
        return mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (a, 0)) for a in addrs])

    def test_public_name_resolving_to_private_address_is_refused(self):
        with self.resolve("127.0.0.1"):
            self.assertFalse(fetch_about.safe_host("looks-public.example.com"))
        with self.resolve("93.184.216.34", "10.0.0.5"):
            self.assertFalse(fetch_about.safe_host("mixed.example.com"))
        with self.resolve("169.254.169.254"):
            self.assertFalse(fetch_about.safe_host("meta.example.com"))

    def test_public_name_and_literals(self):
        with self.resolve("93.184.216.34"):
            self.assertTrue(fetch_about.safe_host("example.com"))
        self.assertFalse(fetch_about.safe_host("10.1.2.3"))
        self.assertFalse(fetch_about.safe_host("localhost"))
        self.assertFalse(fetch_about.safe_host("intranet"))

    def test_lookup_failure_is_refused_even_behind_a_proxy(self):
        for env in ({}, {"https_proxy": "http://proxy:8080"}):
            with mock.patch.dict(os.environ, env), mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
                self.assertFalse(fetch_about.safe_host("internal-only.example.com"))

    def test_curl_is_pinned_to_the_checked_address(self):
        import urllib.parse
        u = "https://example.com:8443/x"
        cmd = fetch_about.curl_command(u, urllib.parse.urlsplit(u), ["93.184.216.34"])
        self.assertEqual(cmd[cmd.index("--resolve") + 1], "example.com:8443:93.184.216.34")
        self.assertEqual(cmd[-1], u)
        cmd6 = fetch_about.curl_command("http://example.com/", urllib.parse.urlsplit("http://example.com/"), ["2606:4700::1"])
        self.assertEqual(cmd6[cmd6.index("--resolve") + 1], "example.com:80:[2606:4700::1]")
        lit = fetch_about.curl_command("http://93.184.216.34/", urllib.parse.urlsplit("http://93.184.216.34/"), ["93.184.216.34"])
        self.assertNotIn("--resolve", lit)

    def test_fetch_refuses_before_running_curl_when_a_name_resolves_private(self):
        with self.resolve("10.0.0.1"), mock.patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                fetch_about.fetch("https://rebind.example.com/")
        run.assert_not_called()

    def test_html_text_join_keeps_inline_markup_tight(self):
        _, text = fetch_about.parse("<html><body><p>This site is made possible by member <a href='/s'>support</a>.</p>"
                                    "<p>Next</p>日本<b>語</b>のページ</body></html>")
        self.assertIn("member support.", judge._norm(text))  # "support ." (link boundary) == "support."
        self.assertIn("日本語のページ", text)  # <b> inside a word does not split it
        self.assertIn("Next", text)
        _, nav = fetch_about.parse("<nav><a href='/'>Home</a><a href='/about'>About</a></nav>")
        self.assertIn("Home About", nav)  # sibling links are separate words

    def test_idn_host_is_pinned_by_its_punycode_name(self):
        import urllib.parse
        u = "https://日本語.example/x"
        cmd = fetch_about.curl_command(u, urllib.parse.urlsplit(u), ["93.184.216.34"])
        puny = "日本語.example".encode("idna").decode()
        self.assertEqual(cmd[cmd.index("--resolve") + 1], f"{puny}:443:93.184.216.34")
        self.assertEqual(cmd[-1], f"https://{puny}/x")  # curl is told to connect by the pinned name


class StoredExperimentRevalidation(unittest.TestCase):
    """The saved results of the 2026-11-14 experiment (arms A-F) must still pass the strict checker.
    var/ is not in git: the test is skipped where the files are absent."""

    def test_all_arms_pass(self):
        root = os.path.dirname(HERE)
        inp = os.path.join(root, "var", "exp-in.jsonl")
        arms = [os.path.join(root, "var", "exp", n) for n in (
            "A-full-n1.json", "B-two-n1.json", "C-two-n5.json", "D-two-n10.json",
            "E-two-n5-nothink.json", "F-two-n5-sonnet.json")]
        if not os.path.exists(inp) or not all(os.path.exists(a) for a in arms):
            self.skipTest("var/exp results not present on this machine")
        with open(inp, encoding="utf-8") as f:
            cands = {json.loads(x)["id"]: json.loads(x) for x in f if x.strip()}
        bad = []
        for path in arms:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for r in data["results"]:
                out = r.get("output")
                if not out or "error" in r:
                    continue
                # a triage-only output has no descriptions; validate_output accepts that shape
                errs = judge.validate_output(out, IDS, judge.render_candidate(cands[r["id"]], 9000))
                if errs:
                    bad.append((os.path.basename(path), r["id"], errs))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
