#!/usr/bin/env python3
"""Auto-approval policy for LLM judgements in the frontier ledger. Issue #129 (decisions: docs/decisions.md 2026-11-18, 2026-11-20).

  uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --dry-run     # counts only
  uv run python3 scripts/llm/approve_policy.py --db var/frontier.sqlite --apply       # update review_state

decide(row) -> "approve" | "hold" | "reject_stale"  (explain(row) also returns the reason).

- approve: spam_verdict pass, confidence high or medium (APPROVE_CONFIDENCES; low is held), both descriptions, a valid category, not public-body-like, and
  (site_kind is business / organization / company, or freshness is active and no deterministic signal contradicts it).
  site_kind individual / unknown / NULL count as a possible individual site: freshness must be active.
- reject_stale: site_kind individual, freshness stale, exempt none, confidence high (medium is never auto-rejected: a human looks), AND a deterministic date
  (freshness_signal_at) exists, is STALE_YEARS or more old, and is not only a copyright year (freshness_source
  footer_copyright is weak). "unknown" and a missing date are doubts, not evidence of a stop: hold.
  review_state=rejected, review_reason=stale_individual. Reversible: review_state.py pending HOST.
- hold: everything else (a human looks): suspect / low confidence, rare_genre / notable exemptions, public-body-like names,
  a judgement made before #129 (freshness NULL) of a possible individual site, stale / unknown without a usable date,
  a contradicting deterministic signal.
Only review_state=pending rows are touched; a row a human approved / rejected is never changed. No network.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts", "pipeline"))
import judge  # noqa: E402
import frontier  # noqa: E402

APPROVE_CONFIDENCES = ("high", "medium")  # confidence values that may be auto-approved (the one place; low is always held). reject_stale stays high-only
STALE_YEARS = 3  # an individual site whose newest sign of life is this many years old or older is "stale" (the one place for this number)

# Public-body-like wording (title / description_ja) and host suffixes. Public bodies are placed by a separate fixed rule
# (docs/concept/listing-criteria.md); the LLM-derived approval never auto-lists them.
PUBLIC_WORDS = ("市立", "県立", "府立", "都立", "道立", "区立", "町立", "村立", "広域連合", "消防", "役場", "市役所", "区役所", "町役場", "村役場",
                "県庁", "都庁", "自治体", "教育委員会", "警察", "保健所", "公民館", "図書館", "議会", "水道局", "広域組合", "一部事務組合")
# 自治体が運営する/設置するサイトを、名前や説明の言い回しで拾う(市立などの語では拾えないもの。
# 例: 「都道府県が設置する若年者向け就職支援施設」「宮崎県日向市への移住を検討する人向けの移住情報サイト」)。
PUBLIC_PATTERNS = (re.compile(r"(都道府県|府|県|市|区|町|村)が(設置|運営|開設|管理)(する|している|した)"),
                   re.compile(r"(市|区|町|村)への(移住|定住)"), re.compile(r"移住(・定住)?(支援|情報|促進)"))
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
    return any(w in text for w in PUBLIC_WORDS) or any(r.search(text) for r in PUBLIC_PATTERNS)


_VALID = None


def valid_categories(raw):
    global _VALID
    if _VALID is None:
        _VALID = (set(judge.category_ids(judge.load_categories())), judge.load_legacy_map())
    ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return bool(judge.modernize_categories(ids, _VALID[0], _VALID[1]))


NON_INDIVIDUAL = ("business", "organization", "company")  # site_kind confirmed as not an individual: freshness is not asked
WEAK_SOURCES = ("footer_copyright",)  # a copyright year alone is weak: it is often left unchanged on a living site


def explain(row, today=None):
    """-> (decision, reason). row: a llm_judgement row (dict-like) plus optional title / freshness_signal_at / freshness_source from frontier."""
    row = dict(row)
    confirmed_other = row.get("site_kind") in NON_INDIVIDUAL  # individual / unknown / NULL are all treated as a possible individual site
    fr, ex = row.get("freshness"), row.get("exempt")
    sig, src = row.get("freshness_signal_at"), row.get("freshness_source")
    if row.get("spam_verdict") != "pass":
        return HOLD, "not_pass"
    if not confirmed_other and fr in ("stale", "unknown"):
        # reject_stale needs evidence of a stop: an individual site, the LLM saying stale, no exemption, high confidence and
        # a deterministic date that is old and not only a copyright year. "unknown" and a missing date are doubts, not evidence.
        if ex != "none":
            return HOLD, "stale_individual_exempt"
        if row.get("site_kind") != "individual" or fr != "stale" or row.get("confidence") != "high":
            return HOLD, "stale_individual_unsure"
        if not sig:
            return HOLD, "stale_without_signal"
        if not signal_is_stale(sig, today):
            return HOLD, "signal_conflict"  # the LLM says stale but a deterministic date is recent
        if src in WEAK_SOURCES:
            return HOLD, "weak_signal"
        return REJECT_STALE, "stale_individual"
    if row.get("confidence") not in APPROVE_CONFIDENCES:
        return HOLD, "low_confidence"
    if not (row.get("description_ja") and row.get("description_en")):
        return HOLD, "no_description"
    if not valid_categories(row.get("categories")):
        return HOLD, "no_valid_category"
    if public_looking(row):
        return HOLD, "public_body"
    if not confirmed_other:
        if fr != "active":
            return HOLD, "freshness_unjudged"
        if sig and signal_is_stale(sig, today):
            return HOLD, "signal_conflict"
    return APPROVE, "ok"


def decide(row, today=None):
    return explain(row, today)[0]


def load_rows(db):
    return db.execute(
        "SELECT j.*, f.title AS title, f.freshness_signal_at AS freshness_signal_at, f.freshness_source AS freshness_source "
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
