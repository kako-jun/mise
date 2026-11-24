"""Stage B "judge" of the online enrichment pipeline: LLM description + review policy, on the production D1 only.

  run(client, limit=N, apply=False)   # dry-run: reads only, no LLM call, no write
  run(client, limit=N, apply=True)    # judge url_progress.state='fetched' rows

Input: url_progress.state='fetched' joined to url_candidates (fetched metadata). Output: the judgement columns of
url_candidates (UPDATE ... WHERE identity_key=?), then the EXISTING deterministic policy (scripts/llm/approve_policy.py,
no new criteria, no popularity) sets review_state and moves url_progress.state:
  approved -> 'approved'   pending (a human looks) -> 'candidate'   rejected -> 'rejected' (review_reason)
  LLM hard failure -> stays 'fetched' (retried next run); after MAX_JUDGE_ATTEMPTS -> 'failed'.
Rows whose review_state is already set are never re-judged (only moved by the policy / their stored state).

Prompts, schemas and validation are the repo's own (scripts/llm/pipeline.py: triage = haiku, describe = sonnet; judge.py
validate_output). Money: every request reserves its worst case in D1 llm_ledger BEFORE the call (atomic against the month cap)
and settles the same row with the real cost after. Memory-only: no local file is read or written except data/categories.csv.
The API key is read from the environment by judge.http and is never printed or logged.
"""
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _sub in ("scripts/pipeline", "scripts/llm"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
import judge  # noqa: E402
import pipeline as llm_pipeline  # noqa: E402  (scripts/llm/pipeline.py)
import approve_policy  # noqa: E402

MONTHLY_CAP_USD = 100.0          # same cap as scripts/nightly.py --budget-month (UTC month)
MAX_JUDGE_ATTEMPTS = 3           # hard failures before url_progress.state -> 'failed'
GROUP = 5                        # candidates per request (pipeline.py default)
TRIAGE_MODEL = judge.MODELS["haiku"]
DESCRIBE_MODEL = judge.MODELS["sonnet"]
ORIGIN = "online"
MAX_PARAMS = 100                 # D1: bound parameters per statement
SELECT_COLUMNS = ("identity_key", "url", "final_url", "title", "description", "lang", "text_head", "flags", "kana_count",
                  "text_len", "ad_hits", "ext_refs", "last_http_status", "robots_status", "freshness_signal_at",
                  "freshness_source", "hint_leaf", "review_state", "notes")


class BudgetExceeded(Exception):
    """The month cap would be exceeded: stop the run, nothing is called."""


class LedgerSettleError(RuntimeError):
    """A reservation could not be settled/verified. It stays reserved (counted at its worst case); the run stops."""


class LLMRefused(Exception):
    """The API answered with an error (auth, billing, overload ...): nothing was billed. API-wide, never a fault of one row:
    the reservation is released and the run stops without touching any row's attempt counter."""


STOP = "__stop__"                # problem marker: the row was not judged because the run stopped (not an attempt)
CLAIM_MINUTES = 30               # a 'reviewing' row without a judgement older than this may be claimed again


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ ledger (D1)
# The month and the timestamps come from the D1 clock (not the runner's), so a skewed runner cannot dodge the cap.
D1_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
MONTH_WHERE = ("ts >= strftime('%Y-%m-01T00:00:00Z','now') "
               "AND ts < strftime('%Y-%m-01T00:00:00Z','now','start of month','+1 month')")


class Ledger:
    def __init__(self, client, run_id, cap=MONTHLY_CAP_USD):
        self.client, self.run_id, self.cap = client, run_id, cap
        self.spent = 0.0  # settled by this run
        self._lock = threading.Lock()

    def month_total(self):
        rows = self.client.query(f"SELECT COALESCE(SUM(cost_usd),0) AS usd FROM llm_ledger WHERE {MONTH_WHERE}")
        return float(rows[0]["usd"]) if rows else 0.0

    def reserve(self, usd, model, note=""):
        """One atomic INSERT ... SELECT ... WHERE month-to-date + usd <= cap, then read it back."""
        usd, rid = round(usd, 6), uuid.uuid4().hex
        self.client.query(
            f"INSERT INTO llm_ledger (id,ts,run,kind,model,mode,cost_usd,note) SELECT ?,{D1_NOW},?,'reserve',?,'single',?,? "
            f"WHERE COALESCE((SELECT SUM(cost_usd) FROM llm_ledger WHERE {MONTH_WHERE}),0)+? <= ?",
            [rid, self.run_id, model, usd, note, usd, self.cap + 1e-9])
        if not self.client.query("SELECT id FROM llm_ledger WHERE id=?", [rid]):
            raise BudgetExceeded(f"monthly cap {self.cap:.2f} USD would be exceeded by a {usd:.4f} USD reservation")
        return rid

    def _verify(self, rid, kind):
        rows = self.client.query("SELECT kind FROM llm_ledger WHERE id=?", [rid])
        if not rows or rows[0]["kind"] != kind:
            raise LedgerSettleError(f"ledger row {rid} is not '{kind}' after the update; it stays reserved at its worst case")

    def settle(self, rid, usage, cost, note=""):
        try:
            self.client.query(
                "UPDATE llm_ledger SET kind='call', input_tokens=?, output_tokens=?, cache_creation_input_tokens=?, "
                "cache_read_input_tokens=?, cost_usd=?, note=? WHERE id=? AND kind='reserve'",
                [usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0,
                 usage.get("cache_creation_input_tokens", 0) or 0, usage.get("cache_read_input_tokens", 0) or 0,
                 round(cost, 6), note[:200], rid])
        except Exception as e:  # noqa: BLE001 - whatever the transport raised, the reservation must stay
            raise LedgerSettleError(f"settle of ledger row {rid} failed ({type(e).__name__}); reservation kept") from e
        self._verify(rid, "call")
        with self._lock:
            self.spent += cost

    def release(self, rid, note=""):
        """Only for an explicit API refusal (nothing billed). A failure here keeps the reservation (conservative)."""
        try:
            self.client.query("UPDATE llm_ledger SET kind='release', cost_usd=0, note=? WHERE id=? AND kind='reserve'", [note[:200], rid])
        except Exception as e:  # noqa: BLE001
            raise LedgerSettleError(f"release of ledger row {rid} failed ({type(e).__name__}); reservation kept") from e
        self._verify(rid, "release")


# ------------------------------------------------------------------ candidates
def candidate_from_row(r):
    try:
        flags = json.loads(r["flags"]) if r.get("flags") else {}
    except (TypeError, ValueError):
        flags = {}
    feats = {"kana_count": r.get("kana_count"), "text_len": r.get("text_len"), "ad_hits": r.get("ad_hits"),
             "external_refs": r.get("ext_refs"), "http_status": r.get("last_http_status"),
             "robots": r.get("robots_status"), "flags": flags}
    if r.get("freshness_signal_at"):
        feats["freshness_signal_at"] = r["freshness_signal_at"]
        feats["freshness_source"] = r.get("freshness_source")
    if r.get("hint_leaf"):
        feats["leaf_hint"] = r["hint_leaf"]
    return {"id": r["identity_key"], "url": r.get("final_url") or r.get("url"), "title": r.get("title"),
            "meta_description": r.get("description"), "lang": r.get("lang"), "body_excerpt": r.get("text_head") or "",
            "features": feats}


def attempts_of(notes):
    m = re.match(r"judge_attempts=(\d+)", notes or "")
    return int(m.group(1)) if m else 0


def input_hash(model, text):
    blob = json.dumps({"origin": ORIGIN, "model": model, "text": text}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ LLM plumbing
def default_llm(params):
    """The repo's own Anthropic call (judge.http). Error text holds the response body only, never a header."""
    try:
        return judge.http("POST", judge.API, params)
    except judge.AmbiguousRequest:
        raise
    except SystemExit as e:
        raise LLMRefused(str(e)[:300]) from None


class Ctx:
    def __init__(self, llm, ledger, log, cats):
        self.llm, self.ledger, self.log = llm, ledger, log
        self.valid_ids = set(judge.category_ids(cats))
        self.legacy = judge.load_legacy_map()
        self.systems = {"triage": llm_pipeline.triage_system(cats), "describe": llm_pipeline.describe_system(cats)}
        self.schemas = {s: llm_pipeline.wrapper_schema(s, cats) for s in self.systems}
        self.lock = threading.Lock()
        self.consecutive_failures = 0
        self.stop = None          # reason string once the run must make no new call (budget, API-wide refusal, ledger error)


def group_text(chunk):
    parts = []
    for n, (c, extra) in enumerate(chunk, 1):
        t = judge.render_candidate(c)
        if extra:
            t = t.replace("<candidate>\n", "<candidate>\n" + extra + "\n", 1)
        parts.append(t.replace("<candidate>", f'<candidate i="{n}">', 1))
    return "\n".join(parts)


def build_request(ctx, stage, model, chunk, max_tokens):
    out = {"format": {"type": "json_schema", "schema": ctx.schemas[stage]}}
    if stage == "triage":
        out["effort"] = "low"
    return {"model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": ctx.systems[stage], "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": group_text(chunk)}], "output_config": out}


def call(ctx, stage, model, chunk, max_tokens=None):
    """reserve -> call -> settle. Returns (msg|None, problem|None). Raises BudgetExceeded / LedgerSettleError."""
    mt = max_tokens or min(16000, llm_pipeline.PER_ITEM_TOKENS[stage] * len(chunk) + llm_pipeline.THINKING_BUFFER[stage])
    params = build_request(ctx, stage, model, chunk, mt)
    try:
        wc, _ = judge.worst_case(model, ctx.systems[stage] + json.dumps(ctx.schemas[stage]), group_text(chunk), mt)
    except judge.BudgetExceeded as e:
        return None, f"prompt too large: {e}"[:200]
    if ctx.stop:
        raise BudgetExceeded(ctx.stop)
    rid = ctx.ledger.reserve(wc, model, f"{stage}x{len(chunk)}")
    try:
        msg = ctx.llm(params)
    except LLMRefused as e:
        try:
            ctx.ledger.release(rid, f"refused: {e}")
        except LedgerSettleError:
            ctx.stop = "ledger error"
            raise
        ctx.stop = f"API refused the request: {e}"[:200]
        raise BudgetExceeded(ctx.stop) from None
    except (Exception, judge.AmbiguousRequest) as e:  # noqa: BLE001 - no answer (SystemExit subclass too): may have been billed, the reservation stays
        ctx.log(f"judge: {stage} request got no answer ({type(e).__name__}); reservation {rid} kept")
        return None, f"no answer: {type(e).__name__}"
    usage = msg.get("usage", {}) or {}
    try:
        ctx.ledger.settle(rid, usage, judge.usage_cost(model, usage), f"{stage}x{len(chunk)}")
    except LedgerSettleError:
        ctx.stop = "ledger error"   # other workers must not keep spending
        raise
    return msg, None


def parse(ctx, stage, work, msg):
    """work = [(cand, extra, base)] -> {pos: (full output|None, problem|None)} (same checks as pipeline.py)."""
    obj, problem = judge.parse_response_text(msg)
    if obj is None or not isinstance(obj.get("r"), list):
        return {n: (None, problem or "no result array") for n in range(len(work))}
    by_i = {}
    for it in obj["r"]:
        if isinstance(it, dict) and isinstance(it.get("i"), int):
            by_i.setdefault(it["i"], it)
    res = {}
    for n, (c, _extra, base) in enumerate(work):
        item = by_i.get(n + 1)
        res[n] = (llm_pipeline.check_item(stage, item, n + 1, ctx.valid_ids, judge.render_candidate(c), base)
                  if item is not None else (None, "missing from response"))
    return res


def run_stage(ctx, stage, model, work):
    """work = [(cand, extra, base)] sent as one request. Returns [(output|None, problem|None)]; an invalid item gets one retry."""
    pairs = [(c, e) for c, e, _ in work]
    try:
        msg, problem = call(ctx, stage, model, pairs)
    except BudgetExceeded as e:
        ctx.stop = ctx.stop or str(e)
        return [(None, STOP)] * len(work)
    if msg is None:
        return [(None, problem)] * len(work)
    res = parse(ctx, stage, work, msg)
    out = [res[n] for n in range(len(work))]
    for n, (o, p) in enumerate(out):
        if o is None:
            mt = None
            if p and "truncated" in p:
                mt = min(16000, 2 * (llm_pipeline.PER_ITEM_TOKENS[stage] + llm_pipeline.THINKING_BUFFER[stage]))
            try:
                m2, p2 = call(ctx, stage, model, [pairs[n]], mt)
            except BudgetExceeded as e:
                ctx.stop = ctx.stop or str(e)
                out[n] = (None, STOP)
                continue
            out[n] = (None, p2) if m2 is None else parse(ctx, stage, [work[n]], m2)[0]
    return out


def judge_chunk(ctx, cands):
    """-> [(full output|None, model, problem)] for the chunk (triage, then describe for the passes)."""
    tri = run_stage(ctx, "triage", TRIAGE_MODEL, [(c, None, None) for c in cands])
    results = [(None, TRIAGE_MODEL, p) for _, p in tri]
    work, idx = [], []
    for n, (t, _p) in enumerate(tri):
        if t is None:
            continue
        if t["spam"]["verdict"] == "pass":
            work.append((cands[n], f"pre-judged: adult={t['adult']['value'][0]}", t))
            idx.append(n)
        else:
            results[n] = (t, TRIAGE_MODEL, None)
    if work:
        for n, (o, p) in zip(idx, run_stage(ctx, "describe", DESCRIBE_MODEL, work)):
            results[n] = (o, f"{TRIAGE_MODEL}+{DESCRIBE_MODEL}", None) if o else (None, DESCRIBE_MODEL, p)
    return results


# ------------------------------------------------------------------ policy / writes
def decide(row):
    """row: judgement fields of one candidate (approve_policy shape) -> (review_state, review_reason, progress_state)."""
    if row.get("spam_verdict") == "reject":
        return "rejected", "spam:" + str(row.get("spam_kind") or "other"), "rejected"
    d, why = approve_policy.explain(row)
    if d == approve_policy.APPROVE:
        return "approved", "auto_approved", "approved"
    if d == approve_policy.REJECT_STALE:
        return "rejected", why, "rejected"
    return "pending", why, "candidate"


JUDGE_SQL = ("UPDATE url_candidates SET input_hash=?, origin=?, model=?, judged_at=?, imported_at=?, spam_verdict=?, spam_kind=?, "
             "spam_reason=?, description_ja=?, description_en=?, long_description_ja=?, long_description_en=?, "
             "long_description_evidence=?, categories=?, tags=?, adult=?, site_kind=?, site_kind_reason=?, site_scope=?, "
             "confidence=?, country_code=?, region_code=?, notes=?, review_state=?, reviewed_at=?, freshness=?, exempt=?, "
             "review_reason=?, updated_at=datetime('now') WHERE identity_key=? AND (review_state IS NULL OR review_state='')")
# A row is claimed ('fetched' -> 'reviewing', compare-and-set) before any LLM call; every write below is guarded by 'reviewing'.
PROGRESS_SQL = "UPDATE url_progress SET state=?, updated_at=datetime('now') WHERE identity_key=? AND state='reviewing'"
CLAIM_SQL = ("UPDATE url_progress SET state='reviewing', updated_at=datetime('now') WHERE identity_key=? AND "
             "(state='fetched' OR (state='reviewing' AND updated_at < datetime('now','-" + str(CLAIM_MINUTES) + " minutes') "
             "AND NOT EXISTS (SELECT 1 FROM url_candidates c WHERE c.identity_key=url_progress.identity_key AND c.review_state IS NOT NULL "
             "AND c.review_state<>''))) RETURNING identity_key")


def judged_statements(row, cand, out, model, stamp, ctx):
    out = dict(out, categories=judge.canonical_categories(out["categories"], ctx.valid_ids, ctx.legacy))
    hide = out["adult"]["value"] == "yes"  # no long body-derived text for adult sites (same policy as import_results.py)
    loc = out["location"]
    cats = json.dumps(out["categories"])
    rs, why, ps = decide({
        "key": row["identity_key"], "title": row.get("title"), "spam_verdict": out["spam"]["verdict"],
        "spam_kind": out["spam"]["kind"], "confidence": out["confidence"], "description_ja": out["description_ja"],
        "description_en": out["description_en"], "categories": cats, "site_kind": out["site_kind"],
        "freshness": out["freshness"], "exempt": out["exempt"], "freshness_signal_at": row.get("freshness_signal_at"),
        "freshness_source": row.get("freshness_source")})
    params = [input_hash(model, judge.render_candidate(cand)), ORIGIN, model, stamp, stamp,
              out["spam"]["verdict"], out["spam"]["kind"], out["spam"]["reason"], out["description_ja"], out["description_en"],
              None if hide else out["long_description_ja"], None if hide else out["long_description_en"],
              None if hide else json.dumps(out["long_description_evidence"], ensure_ascii=False),
              cats, json.dumps(out["tags"], ensure_ascii=False), out["adult"]["value"], out["site_kind"],
              out["site_kind_reason"], out["site_scope"], out["confidence"], loc["country_code"], loc["region_code"],
              out["notes"], rs, stamp if rs in ("approved", "rejected") else None, out["freshness"], out["exempt"], why,
              row["identity_key"]]
    return [(JUDGE_SQL, params), (PROGRESS_SQL, [ps, row["identity_key"]])], ps


def failure_statements(row, problem):
    """A row-specific failure (invalid output, truncation, refusal, no answer, write failure): count one attempt, give the claim back.
    After MAX_JUDGE_ATTEMPTS: 'failed', except a model refusal (stop_reason=refusal) which goes to a human ('candidate')."""
    n = attempts_of(row.get("notes")) + 1
    note = f"judge_attempts={n}; {problem}"[:300]
    final = n >= MAX_JUDGE_ATTEMPTS
    guard = "WHERE identity_key=? AND (review_state IS NULL OR review_state='')"
    k = row["identity_key"]
    if final and "stop_reason=refusal" in problem:
        return [("UPDATE url_candidates SET notes=?, review_state='pending', review_reason='judge_refused', "
                 "updated_at=datetime('now') " + guard, [note, k]), (PROGRESS_SQL, ["candidate", k])], "candidate"
    sql = ("UPDATE url_candidates SET notes=?" + (", review_reason='judge_failed'" if final else "") +
           ", updated_at=datetime('now') " + guard)
    return [(sql, [note, k]), (PROGRESS_SQL, ["failed" if final else "fetched", k])], ("failed" if final else "retry")


def unclaim_statements(row):
    return [(PROGRESS_SQL, ["fetched", row["identity_key"]])]


# A row that already carries a review_state is NOT claimed: its move is deterministic and idempotent, guarded by 'fetched' only,
# so a run that dies half way leaves it 'fetched' (never a 'reviewing' nobody picks up).
MOVE_SQL = "UPDATE url_progress SET state=?, updated_at=datetime('now') WHERE identity_key=? AND state='fetched'"


def transition_statements(row):
    """A row that already carries a review_state: no LLM, only the move (pending goes through the policy again)."""
    rs = row["review_state"]
    if rs in ("approved", "rejected"):
        return [(MOVE_SQL, [rs, row["identity_key"]])]
    nrs, why, ps = decide(dict(row, key=row["identity_key"]))
    stmts = []
    if nrs != "pending":
        stmts.append(("UPDATE url_candidates SET review_state=?, reviewed_at=?, review_reason=?, updated_at=datetime('now') "
                      "WHERE identity_key=? AND review_state='pending'", [nrs, now_iso(), why, row["identity_key"]]))
    stmts.append((MOVE_SQL, [ps, row["identity_key"]]))
    return stmts


def write(client, stmts):
    assert all(len(p) <= MAX_PARAMS for _, p in stmts)
    client.batch(stmts)


def claim(client, rows):
    """Compare-and-set the rows to 'reviewing'; returns only the rows this runner won."""
    won = set()
    for i in range(0, len(rows), 50):
        part = rows[i:i + 50]
        for res in client.batch([(CLAIM_SQL, [r["identity_key"]]) for r in part]):
            won.update(x["identity_key"] for x in res)
    return [r for r in rows if r["identity_key"] in won]


def states_of(client, keys):
    out = {}
    for i in range(0, len(keys), 90):
        part = keys[i:i + 90]
        for r in client.query(f"SELECT identity_key, state FROM url_progress WHERE identity_key IN ({','.join('?' * len(part))})", part):
            out[r["identity_key"]] = r["state"]
    return out


# ------------------------------------------------------------------ run
def run(client, *, limit, apply, log=print, llm=None, budget_month=MONTHLY_CAP_USD, concurrency=1, heartbeat=None):
    stats = {"selected": 0, "judged": 0, "approved": 0, "candidate": 0, "rejected": 0, "failed": 0, "retry_later": 0,
             "moved_without_llm": 0, "write_failed": 0, "lost_claim": 0, "cost_usd": 0.0, "budget_stopped": False,
             "stopped": None, "dry_run": not apply}
    cols = ", ".join(("p." if c in ("identity_key", "url") else "c.") + c for c in SELECT_COLUMNS)
    # review_state/notes and the policy inputs of an already judged row come from the same row
    cols += ", c.categories, c.spam_verdict, c.spam_kind, c.confidence, c.description_ja, c.description_en, c.site_kind, c.freshness, c.exempt"
    rows = client.query(
        f"SELECT {cols} FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key "
        f"WHERE p.state='fetched' OR (p.state='reviewing' AND p.updated_at < datetime('now','-{CLAIM_MINUTES} minutes') "
        "AND (c.review_state IS NULL OR c.review_state='')) ORDER BY p.identity_key LIMIT ?", [int(limit)])
    stats["selected"] = len(rows)
    ledger = Ledger(client, judge.new_run_id(), budget_month)
    todo = [r for r in rows if not r.get("review_state")]
    if not apply:
        stats["would_call_llm_for"] = len(todo)
        stats["would_move_without_llm"] = len(rows) - len(todo)
        stats["month_spent_usd"] = round(ledger.month_total(), 6)
        log(f"judge dry-run: {stats}")
        return stats
    if llm is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set (the value is never printed)")
        llm = default_llm
    ctx = Ctx(llm, ledger, log, judge.load_categories())
    fatal = []

    def count(kind):
        with ctx.lock:
            stats[kind] += 1

    def best_effort(stmts):
        try:
            write(client, stmts)
        except Exception as e:  # noqa: BLE001 - the claim then expires by itself after CLAIM_MINUTES
            log(f"judge: cleanup write failed ({type(e).__name__})")

    for r in rows:
        if not r.get("review_state"):
            continue
        try:
            write(client, transition_statements(r))
            count("moved_without_llm")
        except Exception as e:  # noqa: BLE001 - the row stays 'fetched' and is moved by the next run
            log(f"judge: move of {r['identity_key']} failed ({type(e).__name__})")
            count("write_failed")

    def beat():
        """-> True to go on. heartbeat() raising (lease lost) stops the run; the error is re-raised at the end."""
        if heartbeat is None:
            return True
        try:
            heartbeat()
            return True
        except Exception as e:  # noqa: BLE001
            ctx.stop = ctx.stop or "heartbeat"
            fatal.append(e)
            return False

    def do_chunk(chunk):
        if ctx.stop or ctx.consecutive_failures >= 3:
            return
        if not beat():
            return
        claimed = []
        try:
            claimed = claim(client, chunk)
            if claimed:
                _judge_and_write(claimed)
        except LedgerSettleError:
            raise
        except Exception as e:  # noqa: BLE001 - a D1 error around reserve/claim/read-back: give the claim back, stop, report
            ctx.stop = ctx.stop or f"D1 error ({type(e).__name__})"
            log(f"judge: stopping on {type(e).__name__}; rows given back")
            best_effort([s for r in claimed for s in unclaim_statements(r)])

    def _judge_and_write(chunk):
        cands = [candidate_from_row(r) for r in chunk]
        try:
            results = judge_chunk(ctx, cands)
        except LedgerSettleError as e:
            ctx.stop = ctx.stop or "ledger error"
            fatal.append(e)
            best_effort([s for r in chunk for s in unclaim_statements(r)])
            return
        stmts, kinds, stamp = [], [], now_iso()
        for r, c, (out, model, problem) in zip(chunk, cands, results):
            if out is not None:
                s, ps = judged_statements(r, c, out, model, stamp, ctx)
                kinds.append(("judged", ps))
            elif problem == STOP:
                s = unclaim_statements(r)
                kinds.append(("stopped", None))
            else:
                s, k = failure_statements(r, problem or "unknown")
                kinds.append((k, None))
            stmts += s
        with ctx.lock:
            if any(k == "judged" for k, _ in kinds):
                ctx.consecutive_failures = 0
            elif any(k != "stopped" for k, _ in kinds):
                ctx.consecutive_failures += 1
        if not beat():   # lease lost: no more writes (the claims expire by themselves; the cost is already settled)
            return
        try:
            write(client, stmts)
        except Exception as e:  # noqa: BLE001 - the cost is already settled; count the attempt, rows stay 'fetched'
            log(f"judge: write of {len(chunk)} rows failed ({type(e).__name__}); counting an attempt")
            for r, (kind, _ps) in zip(chunk, kinds):
                count("write_failed")
                # only rows that were really judged (or failed on their own) count an attempt; rows left undone by a stop do not
                best_effort(unclaim_statements(r) if kind == "stopped" else failure_statements(r, "write failed")[0])
            return
        want = {r["identity_key"]: {"judged": ps, "failed": "failed", "candidate": "candidate"}.get(k, "fetched")
                for r, (k, ps) in zip(chunk, kinds)}
        have = states_of(client, list(want))
        for r, (kind, ps) in zip(chunk, kinds):
            if have.get(r["identity_key"]) != want[r["identity_key"]] and kind != "stopped":
                count("lost_claim")      # someone else moved the row: not ours to count
            elif kind == "judged":
                count("judged")
                count(ps)
            elif kind in ("failed", "candidate"):
                count(kind)
            elif kind == "retry":
                count("retry_later")

    chunks = [todo[i:i + GROUP] for i in range(0, len(todo), GROUP)]
    if concurrency > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(concurrency, 4)) as ex:
            for f in [ex.submit(do_chunk, c) for c in chunks]:
                f.result()
    else:
        for c in chunks:
            do_chunk(c)
    stats["cost_usd"] = round(ledger.spent, 6)
    if not ctx.stop and ctx.consecutive_failures >= 3:
        ctx.stop = "3 consecutive requests without a usable result"
    stats["stopped"] = ctx.stop
    stats["budget_stopped"] = bool(ctx.stop) and ctx.stop.startswith("monthly cap")
    log(f"judge: {stats}")
    if fatal:
        raise fatal[0]
    return stats
