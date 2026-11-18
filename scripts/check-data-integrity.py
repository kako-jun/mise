#!/usr/bin/env python3
"""Read-only data integrity check of the sites tables (Issue #137). The checks themselves live in scripts/llm/integrity.py.

  uv run python3 scripts/check-data-integrity.py --local  [--persist-to DIR]   # local D1 (wrangler d1 execute --local)
  uv run python3 scripts/check-data-integrity.py --remote                       # production D1: SELECT only
  uv run python3 scripts/check-data-integrity.py --sqlite FILE                  # a sqlite file

Every check is one simple SELECT, run on its own (production D1 limits compound SELECT terms). 0 rows is correct. Exit code 1 when
any "error" check has rows, 0 otherwise ("warn" checks are shown but never fail). Summary on stdout.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "llm"))
import integrity  # noqa: E402


def wrangler_execute(db, target, persist_to):
    def run(sql):
        cmd = ["npx", "wrangler", "d1", "execute", db, target, "--json", "--command", sql]
        if persist_to and target == "--local":
            cmd += ["--persist-to", persist_to]
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"wrangler failed: {(p.stderr or p.stdout).strip()[-600:]}")
        data = json.loads(p.stdout)
        return [tuple(r.values()) for r in data[0].get("results", [])]
    return run


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print-sql", action="store_true", help="print {check name: SQL} as JSON and exit (tests/category-counts.test.mjs uses it)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--local", action="store_true")
    g.add_argument("--remote", action="store_true")
    g.add_argument("--sqlite", metavar="FILE")
    ap.add_argument("--db", default="mise", help="D1 database name (default mise)")
    ap.add_argument("--persist-to", help="with --local: the wrangler persistence directory")
    ap.add_argument("--limit", type=int, default=5, help="rows to print per failing check")
    a = ap.parse_args(argv)
    if a.print_sql:
        print(json.dumps({n: sql for n, _s, _w, sql in integrity.CHECKS}))
        return 0
    if not (a.local or a.remote or a.sqlite):
        ap.error("one of --local / --remote / --sqlite is required")
    if a.sqlite:
        con = sqlite3.connect("file:" + os.path.abspath(a.sqlite) + "?mode=ro", uri=True)
        execute = integrity.sqlite_execute(con)
    else:
        execute = wrangler_execute(a.db, "--remote" if a.remote else "--local", a.persist_to)
    result = integrity.run_checks(execute)
    failed = 0
    for name, sev, why, _sql in integrity.CHECKS:
        rows = result[name]
        mark = "ok  " if not rows else ("FAIL" if sev == "error" else "warn")
        print(f"[{mark}] {name}: {len(rows)} row(s) -- {why}")
        for r in rows[:a.limit]:
            print(f"        {r}")
        failed += bool(rows) and sev == "error"
    print(f"{failed} check(s) failed" if failed else "all error-level checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
