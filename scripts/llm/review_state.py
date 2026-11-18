#!/usr/bin/env python3
"""Approve / reject / list LLM judgements in the frontier ledger (llm_judgement.review_state). Issue #48.

  review_state.py list [--state pending|approved|rejected] [--verdict pass|suspect|reject|deferred] [--limit N]
  review_state.py approve HOST [HOST ...]
  review_state.py reject HOST [HOST ...]
  review_state.py pending HOST [HOST ...]      # undo

HOST is the ledger key (a leading www. and case are ignored). Nothing is published here: export_approved_sql.py reads
the approved rows. Approval is refused for verdict reject / deferred (not listable) and for a row without a short
description (the sites table needs description_ja). No network.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts", "pipeline"))
import frontier  # noqa: E402

STATES = ("pending", "approved", "rejected")


def norm_key(s):
    s = s.strip().lower()
    return s[4:] if s.startswith("www.") else s


def refusal(row):
    """Why this row cannot be approved (None = fine)."""
    if row["spam_verdict"] in ("reject", "deferred"):
        return f"verdict {row['spam_verdict']} is not listable"
    if not (row["description_ja"] and row["description_en"]):
        return "no description_ja / description_en"
    return None


def set_state(db, keys, state):
    """Returns [(key, ok, message)]."""
    out = []
    for raw in keys:
        key = norm_key(raw)
        row = db.execute("SELECT * FROM llm_judgement WHERE key=?", (key,)).fetchone()
        if row is None:
            out.append((key, False, "no judgement in the ledger"))
            continue
        why = refusal(row) if state == "approved" else None
        if why:
            out.append((key, False, "refused: " + why))
            continue
        db.execute("UPDATE llm_judgement SET review_state=?, reviewed_at=?, review_reason=NULL WHERE key=?",
                   (state, None if state == "pending" else frontier.now(), key))
        out.append((key, True, state))
    db.commit()
    return out


def list_rows(db, state=None, verdict=None, limit=None):
    sql, args = "SELECT * FROM llm_judgement WHERE 1=1", []
    if state:
        sql += " AND review_state=?"; args.append(state)
    if verdict:
        sql += " AND spam_verdict=?"; args.append(verdict)
    sql += " ORDER BY key"
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    return db.execute(sql, args).fetchall()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--state", choices=STATES)
    ls.add_argument("--verdict", choices=("pass", "suspect", "reject", "deferred"))
    ls.add_argument("--limit", type=int)
    for name in ("approve", "reject", "pending"):
        sub.add_parser(name).add_argument("hosts", nargs="+")
    a = ap.parse_args(argv)
    db = frontier.connect(a.db)
    try:
        if a.cmd == "list":
            rows = list_rows(db, a.state, a.verdict, a.limit)
            for r in rows:
                print("\t".join([r["key"], r["review_state"], r["spam_verdict"], r["spam_kind"], r["confidence"],
                                 "adult=" + r["adult"], r["site_scope"], ",".join(json.loads(r["categories"])),
                                 r["description_ja"] or ""]))
            print(f"{len(rows)} row(s)", file=sys.stderr)
            return 0
        state = {"approve": "approved", "reject": "rejected", "pending": "pending"}[a.cmd]
        res = set_state(db, a.hosts, state)
    finally:
        db.close()
    for key, ok, msg in res:
        print(f"{'OK' if ok else 'FAILED'}\t{key}\t{msg}")
    return 0 if all(ok for _, ok, _ in res) else 1


if __name__ == "__main__":
    sys.exit(main())
