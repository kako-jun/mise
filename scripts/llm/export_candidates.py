#!/usr/bin/env python3
"""Export frontier candidates (state='candidate') as judge.py input JSONL.

Read-only: the frontier database is opened with SQLite's read-only mode and never
modified. No network access. Use --db to point at a frontier.sqlite produced by the
discovery pipeline (scripts/pipeline/schema.sql in that work).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cand_id(key):
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)[:64]


def export(db_path, state="candidate"):
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("SELECT * FROM frontier WHERE state=? ORDER BY key", (state,)):
        flags = json.loads(r["flags"]) if r["flags"] else {}
        # personal_score is deliberately NOT passed: mise's bar is "independent site", not "personal".
        features = {
            "kana_count": r["kana_count"], "text_len": r["text_len"], "ad_hits": r["ad_hits"],
            "external_refs": r["ext_refs"], "http_status": r["last_http_status"],
            "robots": r["robots_status"], "flags": flags,
        }
        out.append({"id": cand_id(r["key"]), "url": r["final_url"] or r["url"], "title": r["title"],
                    "meta_description": r["description"], "lang": r["lang"],
                    "body_excerpt": r["text_head"] or "", "features": features})
    con.close()
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="frontier sqlite (opened read-only)")
    p.add_argument("--state", default="candidate")
    p.add_argument("--out", default=os.path.join(ROOT, "var", "frontier-candidates.jsonl"))
    a = p.parse_args(argv)
    rows = export(a.db, a.state)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for c in rows:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"{len(rows)} candidates -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
