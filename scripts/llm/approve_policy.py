#!/usr/bin/env python3
"""Auto-approval policy for LLM judgements in the frontier ledger. Issue #129 (decisions: docs/decisions.md 2026-11-18).

  uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --dry-run     # counts only
  uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --apply       # update review_state

decide(row) -> "approve" | "hold" | "reject_stale"  (explain(row) also returns the reason).

- approve: spam_verdict pass, confidence high, both descriptions, a valid category, not public-body-like, and
  (site_kind is not individual, or freshness is active and no deterministic signal contradicts it).
- reject_stale: individual, freshness stale/unknown, exempt none (pass + high only; a doubtful call goes to a human).
  review_state=rejected, review_reason=stale_individual. Reversible: review_state.py pending HOST.
- hold: everything else (a human looks): suspect / medium / low, rare_genre / notable exemptions, public-body-like names,
  a judgement made before #129 (freshness NULL) of an individual site, a contradicting deterministic signal.
Only review_state=pending rows are touched; a row a human approved / rejected is never changed. No network.
"""
import argparse
import collections
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts", "pipeline"))
import judge  # noqa: E402
import frontier  # noqa: E402

STALE_YEARS = 3  # an individual site whose newest sign of life is this many years old or older is "stale" (the one place for this number)

# Public-body-like wording (title / description_ja) and host suffixes. Public bodies are placed by a separate fixed rule
# (docs/concept/listing-criteria.md); the LLM-derived approval never auto-lists them.
PUBLIC_WORDS = ("市立", "県立", "府立", "都立", "道立", "区立", "町立", "村立", "広域連合", "消防", "役場", "市役所", "区役所", "町役場", "村役場",
                "県庁", "都庁", "自治体", "教育委員会", "警察", "保健所", "公民館", "図書館", "議会", "水道局", "広域組合", "一部事務組合")
PUBLIC_HOST_SUFFIXES = (".lg.jp", ".go.jp", ".ed.jp", ".gov", ".gov.jp", ".pref.jp")
PUBLIC_HOST_LABELS = ("city", "town", "village", "pref", "vill")  # city.xxx.jp / town.xxx.jp / pref.xxx.jp

APPROVE, HOLD, REJECT_STALE = "approve", "hold", "reject_stale"


def stale_cutoff(today=None, years=STALE_YEARS):
    t = today or datetime.date.today()
    try:
        return t.replace(year=t.year - years)
    except ValueError:  # Feb 29
        return t.replace(year=t.year - years, day=28)


def signal_is_stale(signal_at, today=None):
    """True when the date is exactly STALE_YEARS ago or older (the boundary counts as stale)."""
    return datetime.date.fromisoformat(signal_at[:10]) <= stale_cutoff(today)


def public_looking(row):
    host = (row.get("key") or row.get("host") or "").lower().split("/")[0].removeprefix("www.")
    if any(host.endswith(s) for s in PUBLIC_HOST_SUFFIXES):
        return True
    if host.endswith(".jp") and host.split(".")[0] in PUBLIC_HOST_LABELS:
        return True
    text = (row.get("title") or "") + (row.get("description_ja") or "")
    return any(w in text for w in PUBLIC_WORDS)


_VALID = None


def valid_categories(raw):
    global _VALID
    if _VALID is None:
        _VALID = (set(judge.category_ids(judge.load_categories())), judge.load_legacy_map())
    ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return bool(judge.modernize_categories(ids, _VALID[0], _VALID[1]))


def explain(row, today=None):
    """-> (decision, reason). row: a llm_judgement row (dict-like) plus optional title / freshness_signal_at from frontier."""
    row = dict(row)
    individual = row.get("site_kind") == "individual"
    fr, ex = row.get("freshness"), row.get("exempt")
    sig = row.get("freshness_signal_at")
    if row.get("spam_verdict") != "pass":
        return HOLD, "not_pass"
    if individual and fr in ("stale", "unknown") and ex == "none":
        if row.get("confidence") != "high":
            return HOLD, "stale_individual_unsure"
        if sig and not signal_is_stale(sig, today):
            return HOLD, "signal_conflict"  # the LLM says stale but a deterministic date is recent
        return REJECT_STALE, "stale_individual"
    if individual and fr in ("stale", "unknown"):
        return HOLD, "stale_individual_exempt"
    if row.get("confidence") != "high":
        return HOLD, "not_high_confidence"
    if not (row.get("description_ja") and row.get("description_en")):
        return HOLD, "no_description"
    if not valid_categories(row.get("categories")):
        return HOLD, "no_valid_category"
    if public_looking(row):
        return HOLD, "public_body"
    if individual:
        if fr != "active":
            return HOLD, "freshness_unjudged"
        if sig and signal_is_stale(sig, today):
            return HOLD, "signal_conflict"
    return APPROVE, "ok"


def decide(row, today=None):
    return explain(row, today)[0]


def load_rows(db):
    return db.execute(
        "SELECT j.*, f.title AS title, f.freshness_signal_at AS freshness_signal_at "
        "FROM llm_judgement j LEFT JOIN frontier f ON f.key = j.key WHERE j.review_state='pending' ORDER BY j.key").fetchall()


def run(db, apply=False, today=None):
    """Returns (Counter of (decision, reason), [(key, decision, reason)]). Writes only with apply=True."""
    counts, plan = collections.Counter(), []
    for r in load_rows(db):
        d, why = explain(dict(r), today)
        counts[(d, why)] += 1
        plan.append((r["key"], d, why))
    if apply:
        now = frontier.now()
        for key, d, why in plan:
            if d == APPROVE:
                db.execute("UPDATE llm_judgement SET review_state='approved', reviewed_at=?, review_reason='auto_approved' WHERE key=? AND review_state='pending'", (now, key))
            elif d == REJECT_STALE:
                db.execute("UPDATE llm_judgement SET review_state='rejected', reviewed_at=?, review_reason=? WHERE key=? AND review_state='pending'", (now, why, key))
        db.commit()
    return counts, plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="frontier sqlite (default var/frontier.sqlite)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    db = frontier.connect(a.db)
    try:
        counts, plan = run(db, apply=a.apply)
    finally:
        db.close()
    total = collections.Counter()
    for (d, _), n in counts.items():
        total[d] += n
    print(f"{'applied' if a.apply else 'dry-run'}: " + ", ".join(f"{d}={total[d]}" for d in (APPROVE, HOLD, REJECT_STALE)))
    for (d, why), n in sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  {d:13} {why:26} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
