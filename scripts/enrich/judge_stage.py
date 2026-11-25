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
import unicodedata
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _sub in ("scripts/pipeline", "scripts/llm"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
import judge  # noqa: E402
import pipeline as llm_pipeline  # noqa: E402  (scripts/llm/pipeline.py)
import approve_policy  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "scripts", "enrich"))
import quality  # noqa: E402  (scripts/enrich/quality.py: name / description / tags / categories rules)

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
        self.leaf_ids = quality.leaf_ids_of(list(judge.load_tree()[0].items())) & self.valid_ids
        self.systems = {"triage": llm_pipeline.triage_system(cats, quality=True), "describe": llm_pipeline.describe_system(cats, quality=True)}
        self.quality_retries = 0
        self.schemas = {s: llm_pipeline.wrapper_schema(s, cats, quality=True) for s in self.systems}
        self.need_name = False    # requalify: an LLM site name is required
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


def build_request(ctx, stage, model, chunk, max_tokens, note=None):
    out = {"format": {"type": "json_schema", "schema": ctx.schemas[stage]}}
    if stage == "triage":
        out["effort"] = "low"
    return {"model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": ctx.systems[stage], "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": ((note + "\n\n") if note else "") + group_text(chunk)}], "output_config": out}


def call(ctx, stage, model, chunk, max_tokens=None, note=None):
    """reserve -> call -> settle. Returns (msg|None, problem|None). Raises BudgetExceeded / LedgerSettleError."""
    mt = max_tokens or min(16000, llm_pipeline.PER_ITEM_TOKENS[stage] * len(chunk) + llm_pipeline.THINKING_BUFFER[stage])
    params = build_request(ctx, stage, model, chunk, mt, note)
    try:
        wc, _ = judge.worst_case(model, ctx.systems[stage] + json.dumps(ctx.schemas[stage]), (note or "") + group_text(chunk), mt)
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


def parse(ctx, stage, work, msg, final=False):
    """work = [(cand, extra, base)] -> {pos: (full output|None, problem|None)} (same checks as pipeline.py, plus the quality rules).
    A quality violation is a problem 'quality:<field>: <codes>' (so the item is retried once with the violation stated); with final=True
    (the retry's answer) the output is kept and carries out['quality_hold'] = 'quality:<field>' so the row goes to a human."""
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
        if item is None:
            res[n] = (None, "missing from response")
            continue
        full, problem, q = llm_pipeline.check_item_ex(stage, item, n + 1, ctx.valid_ids, judge.render_candidate(c), base, leaf_ids=ctx.leaf_ids,
                                                     need_name=ctx.need_name)
        if full is not None and q:
            field = quality.which(q[0])
            if not final:
                res[n] = (None, f"quality:{field}: {', '.join(q)}")
                continue
            full["quality_hold"] = f"quality:{field}"
        res[n] = (full, problem)
    return res


def run_stage(ctx, stage, model, work, note=None):
    """work = [(cand, extra, base)] sent as one request. Returns [(output|None, problem|None)]; an invalid item gets one retry."""
    pairs = [(c, e) for c, e, _ in work]
    try:
        msg, problem = call(ctx, stage, model, pairs, note=note)
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
            pair, note2 = pairs[n], note
            if p and p.startswith("quality:"):
                with ctx.lock:
                    ctx.quality_retries += 1
                note2 = (((note + "\n") if note else "") + "YOUR PREVIOUS ANSWER WAS REJECTED (" + p + "). Answer again and fix exactly this; "
                         "change nothing else.")
            try:
                m2, p2 = call(ctx, stage, model, [pair], mt, note2)
            except BudgetExceeded as e:
                ctx.stop = ctx.stop or str(e)
                out[n] = (None, STOP)
                continue
            out[n] = (None, p2) if m2 is None else parse(ctx, stage, [work[n]], m2, final=True)[0]
    return out


def judge_chunk(ctx, cands):
    """-> [(full output|None, model, problem)] for the chunk (triage, then describe for the passes)."""
    tri = run_stage(ctx, "triage", TRIAGE_MODEL, [(c, None, None) for c in cands])
    results = [(None, TRIAGE_MODEL, p) for _, p in tri]
    work, idx = [], []
    for n, (t, _p) in enumerate(tri):
        if t is None:
            continue
        if t["spam"]["verdict"] == "pass" and not t.get("quality_hold"):
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
             "review_reason=?, site_name=?, updated_at=datetime('now') WHERE identity_key=? AND (review_state IS NULL OR review_state='')")
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
    hold = out.get("quality_hold")
    rs, why, ps = decide({
        "key": row["identity_key"], "title": row.get("title"), "spam_verdict": out["spam"]["verdict"],
        "spam_kind": out["spam"]["kind"], "confidence": out["confidence"], "description_ja": out["description_ja"],
        "description_en": out["description_en"], "categories": cats, "site_kind": out["site_kind"],
        "freshness": out["freshness"], "exempt": out["exempt"], "freshness_signal_at": row.get("freshness_signal_at"),
        "freshness_source": row.get("freshness_source")})
    note = out["notes"]
    if hold and rs == "approved":     # a quality violation that survived the retry: a human looks (never auto approved); a rejection stays one
        rs, why, ps = "pending", hold, "candidate"
    elif hold and rs == "pending":    # the policy already sends it to a human / the cats: keep ITS reason, only note the violation
        note = (note + " " if note else "") + f"[{hold}]"
    params = [input_hash(model, judge.render_candidate(cand)), ORIGIN, model, stamp, stamp,
              out["spam"]["verdict"], out["spam"]["kind"], out["spam"]["reason"], out["description_ja"], out["description_en"],
              None if hide else out["long_description_ja"], None if hide else out["long_description_en"],
              None if hide else json.dumps(out["long_description_evidence"], ensure_ascii=False),
              cats, json.dumps(out["tags"], ensure_ascii=False), out["adult"]["value"], out["site_kind"],
              out["site_kind_reason"], out["site_scope"], out["confidence"], loc["country_code"], loc["region_code"],
              note, rs, stamp if rs in ("approved", "rejected") else None, out["freshness"], out["exempt"], why,
              out.get("site_name"), row["identity_key"]]
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


# ------------------------------------------------------------------ empty pages (no LLM)
# A page with (almost) no text and no description is rejected deterministically: there is nothing to list yet. This says nothing about
# the kind of site (adult, personal, tiny are all treated the same). A later re-fetch cycle can reopen it (next_fetch_at, see enrich.md).
EMPTY_TEXT_LEN = 80
EMPTY_REASON = "empty_content"
EMPTY_REOPEN_DAYS = 30
EMPTY_CAND_SQL = ("UPDATE url_candidates SET review_state='rejected', review_reason='" + EMPTY_REASON + "', reviewed_at=?, "
                  "next_fetch_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','+" + str(EMPTY_REOPEN_DAYS) + " days'), updated_at=datetime('now') ")


# A title that says nothing about the site. Exact (after folding case/width/trailing punctuation) or, for the long phrases, contained.
PLACEHOLDER_TITLES_EXACT = ("無題", "untitled", "no title", "notitle", "welcome", "home", "index", "top", "トップ", "トップページ",
                            "ホーム", "ようこそ", "new page", "my site", "test", "テスト")
PLACEHOLDER_TITLE_PARTS = ("index of", "まだ記事がありません", "記事がありません", "準備中", "coming soon", "under construction",
                           "工事中", "page not found", "404 not found", "403 forbidden", "ページが見つかりません", "default web site page", "it works", "welcome to nginx", "apache2 default")


def placeholder_title(title):
    """True for an empty title or a generic placeholder (PLACEHOLDER_TITLES_EXACT / PLACEHOLDER_TITLE_PARTS)."""
    t = unicodedata.normalize("NFKC", title or "").strip().casefold().strip(" .!-_|:/")
    return not t or t in PLACEHOLDER_TITLES_EXACT or any(w in t for w in PLACEHOLDER_TITLE_PARTS)


def is_empty_content(row):
    """True only when EVERY condition holds (the safest shape: text_len counts visible body text only, so SPA shells, framesets, image
    galleries, image-text shops, link-only tops, ENTER pages and Flash pages must go to the LLM as before):
      text_len known and < EMPTY_TEXT_LEN; no fetched meta description (`description`); no description of our own (description_ja);
      title empty or a placeholder; no external references (ext_refs 0 / NULL); not public-body looking (the public_body path).
    An unknown text_len is never 'empty'."""
    try:
        n = int(row.get("text_len"))
    except (TypeError, ValueError):
        return False
    if n >= EMPTY_TEXT_LEN or (row.get("description_ja") or "").strip() or (row.get("description") or "").strip():
        return False
    try:
        if int(row.get("ext_refs") or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    if not placeholder_title(row.get("title")):
        return False
    return not approve_policy.public_looking({"key": row["identity_key"], "title": row.get("title"), "description_ja": row.get("description_ja")})


def empty_fetched_statements(row, stamp):
    """A claimed ('reviewing') row that has no review_state yet."""
    k = row["identity_key"]
    return [(EMPTY_CAND_SQL + "WHERE identity_key=? AND (review_state IS NULL OR review_state='')", [stamp, k]), (PROGRESS_SQL, ["rejected", k])]


def empty_pending_statements(row, stamp):
    """An already judged row (state 'candidate', review_state 'pending'); nobody may be voting on it right now."""
    k = row["identity_key"]
    return [(EMPTY_CAND_SQL + "WHERE identity_key=? AND review_state='pending' AND EXISTS (SELECT 1 FROM url_progress WHERE identity_key=? AND state='candidate') "
             "AND NOT EXISTS (SELECT 1 FROM deliberations WHERE identity_key=? AND status='running' AND lease_until >= " + D1_NOW + ")",
             [stamp, k, k, k]),
            ("UPDATE url_progress SET state='rejected', updated_at=datetime('now') WHERE identity_key=? AND state='candidate' AND EXISTS "
             "(SELECT 1 FROM url_candidates WHERE identity_key=? AND review_state='rejected' AND review_reason='" + EMPTY_REASON + "')", [k, k])]


def backfill_empty_content(client, apply, log=print, page=100):
    """One-time-safe backfill: rows already judged ('candidate'/'pending') that are empty pages -> rejected 'empty_content'.
    Idempotent (a rejected row is no longer selected). Dry-run (apply=False) writes nothing and only counts. -> stats dict."""
    stats = {"examined": 0, "empty": 0, "skipped_public_body": 0, "rejected": 0, "lost": 0, "dry_run": not apply}
    last = ""
    while True:
        rows = client.query(
            "SELECT p.identity_key AS identity_key, c.title, c.description, c.description_ja, c.text_len, c.ext_refs FROM url_progress p "
            "JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='candidate' AND c.review_state='pending' "
            "AND c.text_len IS NOT NULL AND c.text_len < ? AND TRIM(COALESCE(c.description_ja,''))='' "
            # rows a human or the cats can still save are not touched: a pass/suspect verdict (the describe-after-approve path),
            # public bodies, anything the deliberation already wrote a reason for, and rows with an approve majority
            "AND COALESCE(c.spam_verdict,'') NOT IN ('pass','suspect') AND COALESCE(c.review_reason,'')<>'public_body' "
            "AND COALESCE(c.review_reason,'') NOT LIKE 'deliberation:%' AND NOT EXISTS (SELECT 1 FROM deliberations d WHERE "
            "d.identity_key=p.identity_key AND (d.shiro_vote='approve')+(d.kuro_vote='approve')+(d.yomogi_vote='approve')>=2) "
            "AND p.identity_key > ? ORDER BY p.identity_key LIMIT ?",
            [EMPTY_TEXT_LEN, last, int(page)])
        if not rows:
            break
        last = rows[-1]["identity_key"]
        stats["examined"] += len(rows)
        hit = [r for r in rows if is_empty_content(r)]
        stats["skipped_public_body"] += len(rows) - len(hit)
        stats["empty"] += len(hit)
        if apply and hit:
            stamp = now_iso()
            for i in range(0, len(hit), 20):
                part = hit[i:i + 20]
                write(client, [s for r in part for s in empty_pending_statements(r, stamp)])
                got = {r["identity_key"] for r in client.query(
                    f"SELECT p.identity_key FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='rejected' "
                    f"AND c.review_state='rejected' AND c.review_reason='{EMPTY_REASON}' AND p.identity_key IN ({','.join('?' * len(part))})",
                    [r["identity_key"] for r in part])}
                stats["rejected"] += len(got)
                stats["lost"] += len(part) - len(got)
    log(f"backfill-empty: {stats}")
    return stats


REOPEN_SQL = [
    # one batch (= one transaction). 1: due rows go back to the fetch queue. 2: the review mark of every empty_content row that is back in
    # the fetch/judge flow is cleared (also rows the fetch stage already claimed, so a re-run picks up what a half done run left).
    ("UPDATE url_progress SET state='discovered', updated_at=datetime('now') WHERE state='rejected' AND identity_key IN "
     "(SELECT identity_key FROM url_candidates WHERE review_reason='" + EMPTY_REASON + "' AND review_state='rejected' "
     "AND next_fetch_at<=" + D1_NOW + ")"),
    ("UPDATE url_candidates SET review_state=NULL, review_reason=NULL, reviewed_at=NULL, updated_at=datetime('now') "
     "WHERE review_reason='" + EMPTY_REASON + "' AND review_state='rejected' AND identity_key IN "
     "(SELECT identity_key FROM url_progress WHERE state IN ('discovered','queued','fetched'))"),
]


def reopen_empty_content(client, apply, log=print):
    """Reopen 'empty_content' rows whose next_fetch_at has passed (next_fetch_at is only a mark; nothing reopens them by itself).
    Dry-run counts only. -> stats."""
    due = client.query("SELECT COUNT(*) AS n FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='rejected' "
                       f"AND c.review_reason='{EMPTY_REASON}' AND c.review_state='rejected' AND c.next_fetch_at<={D1_NOW}")[0]["n"]
    stats = {"due": int(due), "dry_run": not apply}
    if apply:
        client.batch([(q, []) for q in REOPEN_SQL])
    log(f"reopen-empty: {stats}")
    return stats


# ------------------------------------------------------------------ describe-after-approve (shared with the deliberate stage)
def _json_list(v):
    try:
        x = json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return None
    return x if isinstance(x, list) else None


def describe_base(row, categories, ctx):
    """A triage-shaped 'full' output rebuilt from the stored judgement columns, so the describe stage (and its validation) can be reused."""
    adult = row.get("adult") if row.get("adult") in ("yes", "no", "unknown") else "unknown"
    verdict = row.get("spam_verdict")
    kind = row.get("spam_kind") or ("none" if verdict == "pass" else "other")
    return {"description_ja": None, "description_en": None, "long_description_ja": None, "long_description_en": None,
            "long_description_evidence": [],
            "spam": {"verdict": verdict, "kind": kind, "reason": row.get("spam_reason") or "(no reason given)"},
            "categories": categories, "tags": [],
            # adult=yes needs a verbatim quote that is not stored: validate as 'unknown' (the stored value is untouched and still sent as pre-judged)
            "adult": {"value": "unknown" if adult == "yes" else adult, "evidence_quote": None},
            "site_scope": row.get("site_scope") or "standalone", "site_kind": row.get("site_kind") or "unknown",
            "site_kind_reason": row.get("site_kind_reason") or "(stored)", "freshness": row.get("freshness") or "unknown",
            "exempt": row.get("exempt") or "none", "confidence": row.get("confidence") or "low",
            "location": {"country_code": None, "region_code": None, "evidence_quote": None}, "notes": ""}


def defang_row(row):
    """Angle brackets in the site's own text become full-width, so nothing in a page can close <candidate> or open a tag of its own."""
    return {k: (v.replace("<", "＜").replace(">", "＞") if isinstance(v, str) and k in ("title", "description", "text_head", "lang", "url", "final_url",
                                                                                      "hint_leaf", "freshness_source") else v) for k, v in row.items()}


def describe_row(ctx, row, need_categories=False, note=None):
    """Describe ONE already judged row with the judge's own describe prompt / validation / ledger (run_stage -> call -> reserve/settle).
    need_categories: the stored categories are unusable, so a triage call (the same cheap one) supplies them first.
    row = the full joined row (url_progress p + url_candidates c.*). -> (output | None, problem | None); problem == STOP when the run must stop.
    Raises LedgerSettleError. Nothing is written here."""
    cand = candidate_from_row(defang_row(row))
    if need_categories:
        t, p = run_stage(ctx, "triage", TRIAGE_MODEL, [(cand, None, None)])[0]
        if t is None:
            return None, p
        if t["spam"]["verdict"] == "reject" or not t["categories"]:
            return None, "triage: " + ("rejected" if t["spam"]["verdict"] == "reject" else "no category")
        if t.get("quality_hold"):
            return None, t["quality_hold"]
        cats = t["categories"]
    else:
        cats = judge.canonical_categories(_json_list(row.get("categories")) or [], ctx.valid_ids, ctx.legacy)
    base = describe_base(row, cats, ctx)
    adult = row.get("adult") if row.get("adult") in ("yes", "no", "unknown") else "unknown"
    out, p = run_stage(ctx, "describe", DESCRIBE_MODEL, [(cand, f"pre-judged: adult={adult[0]}", base)], note=note)[0]
    if out is None:
        return None, p
    if out.get("quality_hold"):
        return None, out["quality_hold"]
    if not out["description_ja"] or not out["description_en"]:
        return None, "the text supports no description"
    if adult == "yes":    # same policy as judged_statements: no long body-derived text for adult sites
        out.update(long_description_ja=None, long_description_en=None, long_description_evidence=[])
    return out, None


def described_assignments(out, row, regenerate_categories):
    """-> (SET clause, params, overrides dict for an in-memory gate check). Country/region only fill, never erase."""
    loc = out["location"]
    over = {"description_ja": out["description_ja"], "description_en": out["description_en"],
            "long_description_ja": out["long_description_ja"], "long_description_en": out["long_description_en"],
            "long_description_evidence": json.dumps(out["long_description_evidence"], ensure_ascii=False) if out["long_description_evidence"] else None,
            "tags": json.dumps(out["tags"], ensure_ascii=False),
            "country_code": loc["country_code"] or row.get("country_code"), "region_code": loc["region_code"] or row.get("region_code")}
    if out.get("site_name"):
        over["site_name"] = out["site_name"]
    if regenerate_categories:
        over["categories"] = json.dumps(out["categories"])
    sets = ", ".join(f"{k}=?" for k in over)
    return sets, list(over.values()), over


# ------------------------------------------------------------------ requalify (quality violations that need a new describe)
REQUALIFY_MAX_ATTEMPTS = 2       # per row; after that only a human moves it (it never blocks the queue)


def requalify_attempts(notes):
    m = re.match(r"requalify_attempts=(\d+);", notes or "")
    return int(m.group(1)) if m else 0


def _strip_attempts(notes):
    return re.sub(r"^requalify_attempts=\d+;\s*", "", notes or "")


# The attempt count is the prefix 'requalify_attempts=N;' of notes (the old notes follow it). SQL compares it (GLOB), so rows whose attempts are used
# up never take a place in the selection: nothing is discarded in Python after a LIMIT.
ATTEMPTS_LEFT_SQL = ("COALESCE(c.notes,'') NOT GLOB 'requalify_attempts=[" + str(REQUALIFY_MAX_ATTEMPTS) + "-9];*' AND "
                     "COALESCE(c.notes,'') NOT GLOB 'requalify_attempts=[1-9][0-9]*;*'")
REQUALIFY_PAGE = 200
REQUALIFY_SCAN_MAX = 2000        # approved rows looked at per run (clean ones are the publish stage's)
HELD_SQL = ("SELECT p.identity_key AS identity_key, p.state AS progress_state, p.updated_at AS progress_updated_at, c.* FROM url_progress p "
            "JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='candidate' AND c.review_state='pending' "
            "AND (c.review_reason LIKE 'gate:quality_%' OR c.review_reason LIKE 'quality:%') AND " + ATTEMPTS_LEFT_SQL +
            " ORDER BY p.updated_at, p.identity_key LIMIT ?")
APPROVED_SQL = ("SELECT p.identity_key AS identity_key, p.state AS progress_state, p.updated_at AS progress_updated_at, c.* FROM url_progress p "
                "JOIN url_candidates c ON c.identity_key=p.identity_key WHERE p.state='approved' AND c.review_state='approved' AND " + ATTEMPTS_LEFT_SQL +
                " AND (p.updated_at > ? OR (p.updated_at = ? AND p.identity_key > ?)) ORDER BY p.updated_at, p.identity_key LIMIT ?")


def requalify_candidates(client, limit):
    """-> [(row, gate reason | None)]. Eligible: 'candidate' rows held for a quality reason (gate:quality_* from the publish gate, quality:* from the
    judge; attempts left, oldest first) and 'approved' rows the publish gate would hold for a quality reason (scanned page by page from the oldest;
    a clean row is skipped, so a long run of clean rows never hides the one behind it)."""
    import publish_stage
    out = [(r, publish_stage.gate_reason(dict(r))) for r in client.query(HELD_SQL, [int(limit)])]
    seen, after = 0, ("", "")
    while len(out) < limit and seen < REQUALIFY_SCAN_MAX:
        rows = client.query(APPROVED_SQL, [after[0], after[0], after[1], REQUALIFY_PAGE])
        if not rows:
            break
        for r in rows:
            g = publish_stage.gate_reason(dict(r))
            if (g or "").startswith("gate:quality_") and len(out) < limit:
                out.append((r, g))
        seen += len(rows)
        after = (rows[-1]["progress_updated_at"], rows[-1]["identity_key"])
    return out


def requalify_note(gate, row):
    return ("REQUALIFY: the stored result for the candidate below was rejected (" + str(gate or row.get("review_reason")) + "). Answer again so that every "
            "rule passes. " + quality.PROMPT_DESCRIBE_RULES + " " + quality.PROMPT_TRIAGE_RULES)


def requalify(client, *, limit, apply, log=print, llm=None, budget_month=MONTHLY_CAP_USD, heartbeat=None, llm_enabled=True):
    """One describe (and a triage when the categories are unusable) per row whose stored result breaks a quality rule, with the violation
    stated; then the publish gate again on the result. A row the judge held (review_reason 'quality:*') goes through the approval policy
    (decide) again and is promoted only if the policy approves it; a row the publish gate held was approved before and is approved again.
    The original review_reason of a human / deliberation waiting row is never overwritten (the judge keeps it and only notes the violation).
    Fails -> 'candidate'/'pending' with the new reason; the attempt is counted in the notes PREFIX (requalify_attempts=N; the old notes follow),
    max REQUALIFY_MAX_ATTEMPTS, and the row goes to the back of the queue (url_progress.updated_at). A request without an answer (timeout) is not an
    attempt; 3 requests in a row without a usable result stop the run. Same ledger / cap / defanging as the judge."""
    import publish_stage
    stats = {"selected": 0, "requalified": 0, "still_held": 0, "describe_failed": 0, "no_answer": 0, "cost_usd": 0.0, "stopped": None,
             "budget_stopped": False, "dry_run": not apply}
    rows = requalify_candidates(client, limit)
    stats["selected"] = len(rows)
    if not apply or not llm_enabled or not rows:
        log(f"requalify: {stats}")
        return stats
    if llm is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set (the value is never printed)")
        llm = default_llm
    ledger = Ledger(client, judge.new_run_id(), budget_month)
    ctx = Ctx(llm, ledger, log, judge.load_categories())
    failures = 0
    for row, gate in rows:
        if heartbeat:
            heartbeat()
        k, pstate, reason = row["identity_key"], row["progress_state"], row.get("review_reason") or ""
        gate_now = gate or ""
        need_cat = gate_now == "gate:quality_categories" or reason == "quality:categories" or not _leaf_usable(row, ctx)
        ctx.need_name = gate_now == "gate:quality_name"
        n = requalify_attempts(row.get("notes")) + 1
        out, problem, over, gate2 = None, None, {}, gate
        try:
            if gate is not None:
                out, problem = describe_row(ctx, defang_row(row), need_categories=need_cat, note=requalify_note(gate, row))
                if out is not None:
                    _sets, _params, over = described_assignments(out, row, need_cat)
                    gate2 = publish_stage.gate_reason(dict(row, **over))
        except BudgetExceeded as e:
            ctx.stop = ctx.stop or str(e)
            problem = STOP
        except LedgerSettleError:
            stats["stopped"] = "ledger error"
            raise
        if problem == STOP:
            stats["stopped"] = ctx.stop
            stats["budget_stopped"] = bool(ctx.stop) and str(ctx.stop).startswith("monthly cap")
            break
        if gate is not None:
            failures = 0 if out is not None else failures + 1
        if gate is not None and out is None:
            log(f"requalify: describe of {k} failed ({problem})"[:300])
            if (problem or "").startswith("no answer"):
                stats["no_answer"] += 1           # a timeout is not the row's fault: no attempt, no write
                if failures >= 3:
                    stats["stopped"] = "3 consecutive requests without a usable result"
                    break
                continue
            stats["describe_failed"] += 1
        if failures >= 3:
            stats["stopped"] = "3 consecutive requests without a usable result"
        stamp = now_iso()
        rs, why = "approved", "requalified"
        if gate2 is None and reason.startswith("quality:"):      # the judge's own hold: the approval policy decides again, never a promotion by itself
            rs, why, _ps = decide(dict(row, key=k, **over))
        sets = ", ".join(f"{c}=?" for c in over)
        if gate2 is None and rs == "approved":
            stmts = [("UPDATE url_candidates SET " + (sets + ", " if sets else "") + "review_state='approved', reviewed_at=?, review_reason='requalified', "
                      "notes=?, updated_at=datetime('now') WHERE identity_key=? AND EXISTS (SELECT 1 FROM url_progress WHERE identity_key=? AND state=?)",
                      list(over.values()) + [stamp, _strip_attempts(row.get("notes")), k, k, pstate]),
                     ("UPDATE url_progress SET state='approved', updated_at=datetime('now') WHERE identity_key=? AND state=?", [k, pstate])]
            stats["requalified"] += 1
        elif gate2 is None:                                      # fixed, but the policy says a human / the cats look: back to their queue with its own reason
            stmts = [("UPDATE url_candidates SET " + (sets + ", " if sets else "") + "review_state='pending', review_reason=?, notes=?, "
                      "updated_at=datetime('now') WHERE identity_key=? AND EXISTS (SELECT 1 FROM url_progress WHERE identity_key=? AND state=?)",
                      list(over.values()) + [why, _strip_attempts(row.get("notes")), k, k, pstate]),
                     ("UPDATE url_progress SET state='candidate', updated_at=datetime('now') WHERE identity_key=? AND state=?", [k, pstate])]
            stats["requalified"] += 1
        else:
            stmts = [("UPDATE url_candidates SET review_state='pending', review_reason=?, notes=?, updated_at=datetime('now') WHERE identity_key=? "
                      "AND EXISTS (SELECT 1 FROM url_progress WHERE identity_key=? AND state=?)",
                      [gate2 or gate_now, f"requalify_attempts={n}; " + _strip_attempts(row.get("notes")), k, k, pstate]),
                     ("UPDATE url_progress SET state='candidate', updated_at=datetime('now') WHERE identity_key=? AND state=?", [k, pstate])]
            stats["still_held"] += 1
        write(client, stmts)
        if stats["stopped"]:
            break
    stats["cost_usd"] = round(ledger.spent, 6)
    log(f"requalify: {stats}")
    return stats


def _leaf_usable(row, ctx):
    """The stored categories (after the old-id conversion) are non-empty and all leaves."""
    raw = _json_list(row.get("categories"))
    if raw is None:
        return False
    cats = judge.canonical_categories([c for c in raw if isinstance(c, str)], ctx.valid_ids, ctx.legacy)
    return not quality.check_categories(cats, ctx.leaf_ids)


# ------------------------------------------------------------------ run
def run(client, *, limit, apply, log=print, llm=None, budget_month=MONTHLY_CAP_USD, concurrency=1, heartbeat=None, llm_enabled=True, priority=None):
    """llm_enabled=False: never calls the LLM (no key needed); only the deterministic moves of rows that already carry a review_state are done."""
    stats = {"selected": 0, "judged": 0, "approved": 0, "candidate": 0, "rejected": 0, "failed": 0, "retry_later": 0,
             "moved_without_llm": 0, "quality_held": 0, "quality_retries": 0, "empty_rejected": 0, "write_failed": 0, "lost_claim": 0, "cost_usd": 0.0, "budget_stopped": False,
             "stopped": None, "dry_run": not apply}
    cols = ", ".join(("p." if c in ("identity_key", "url") else "c.") + c for c in SELECT_COLUMNS)
    # review_state/notes and the policy inputs of an already judged row come from the same row
    cols += ", c.categories, c.spam_verdict, c.spam_kind, c.confidence, c.description_ja, c.description_en, c.site_kind, c.freshness, c.exempt"
    order, extra = "p.identity_key", ""
    if priority is not None:
        order, extra = priority.order(client, "p.identity_key")
    rows = client.query(
        f"SELECT {cols}{extra} FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key "
        f"WHERE p.state='fetched' OR (p.state='reviewing' AND p.updated_at < datetime('now','-{CLAIM_MINUTES} minutes') "
        "AND (c.review_state IS NULL OR c.review_state='')) ORDER BY " + order + " LIMIT ?", [int(limit)])
    if priority is not None:
        priority.report(rows, log, "judge")
    stats["selected"] = len(rows)
    ledger = Ledger(client, judge.new_run_id(), budget_month)
    todo = [r for r in rows if not r.get("review_state")]
    empties = [r for r in todo if is_empty_content(r)]      # no LLM, ever: decided before any call (also with llm_enabled=False)
    todo = [r for r in todo if not is_empty_content(r)]
    if not llm_enabled:
        stats["llm_disabled"] = True
        if apply:
            todo = []
    if not apply:
        stats["would_call_llm_for"] = len(todo)
        stats["would_reject_empty"] = len(empties)
        stats["would_move_without_llm"] = len(rows) - len(todo) - len(empties)
        stats["month_spent_usd"] = round(ledger.month_total(), 6)
        log(f"judge dry-run: {stats}")
        return stats
    if llm is None and llm_enabled:
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

    for i in range(0, len(empties), 20):
        part = empties[i:i + 20]
        try:
            mine = claim(client, part)
            if mine:
                stamp = now_iso()
                write(client, [s for r in mine for s in empty_fetched_statements(r, stamp)])
                have = states_of(client, [r["identity_key"] for r in mine])
                for r in mine:
                    count("empty_rejected" if have.get(r["identity_key"]) == "rejected" else "lost_claim")
        except Exception as e:  # noqa: BLE001 - claimed rows expire by themselves after CLAIM_MINUTES; the rest stays 'fetched'
            log(f"judge: empty-content rejection failed ({type(e).__name__})")
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
        out_by_key = {r["identity_key"]: res[0] for r, res in zip(chunk, results) if res[0] is not None}
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
        if not beat():
            # lease lost: the cost is already settled, so keep the result with the claim-guarded writes (a row somebody else moved is not
            # touched), then give the rest of the claim back (best effort) so a stale 'reviewing' does not block the next start
            best_effort(stmts)
            best_effort([s for r in chunk for s in unclaim_statements(r)])
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
                if (r_out := out_by_key.get(r["identity_key"])) and r_out.get("quality_hold"):
                    count("quality_held")
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
    stats["quality_retries"] = ctx.quality_retries
    if not ctx.stop and ctx.consecutive_failures >= 3:
        ctx.stop = "3 consecutive requests without a usable result"
    stats["stopped"] = ctx.stop
    stats["budget_stopped"] = bool(ctx.stop) and ctx.stop.startswith("monthly cap")
    log(f"judge: {stats}")
    if fatal:
        raise fatal[0]
    return stats
