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
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "llm"))
import integrity  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "scripts", "enrich"))
import quality  # noqa: E402  (the same rules the judge and the publish gate use; Issue #273)

# Warn-level quality checks (never fail the run). Baseline = production count measured 2026-11-25 (16,915 published sites); the report says
# when a count is above it, so a regression is noticed. The goal after the cleanup is 0 (then lower the baseline to 0).
QUALITY_BASELINE = {"name_too_long": 2603, "description_style": 4232, "tag_label_ja_not_japanese": 58543, "category_non_leaf_only": 1836}
QUALITY_SQL = {
    "name_too_long": f"SELECT id, name FROM sites WHERE length(name) > {quality.NAME_MAX}",
    "description_style": "SELECT id, description_ja FROM sites WHERE description_ja IS NOT NULL AND description_ja<>''",
    "tag_label_ja_not_japanese": "SELECT site_id, label_ja FROM site_tags",
    "category_non_leaf_only": ("SELECT s.id FROM sites s WHERE EXISTS (SELECT 1 FROM site_categories a WHERE a.site_id=s.id) AND NOT EXISTS "
                              "(SELECT 1 FROM site_categories x WHERE x.site_id=s.id AND NOT EXISTS (SELECT 1 FROM categories c WHERE c.parent_id=x.category_id))"),
}
QUALITY_WHY = {
    "name_too_long": f"sites.name (what the publish stage writes) is longer than {quality.NAME_MAX} characters",
    "description_style": "description_ja ends in the plain (常体) form instead of 体言止め / です・ます",
    "tag_label_ja_not_japanese": "a tag whose label_ja has no kana / kanji",
    "category_non_leaf_only": "a site that has only non-leaf (parent) categories",
}


def quality_checks(execute):
    """-> {name: [violating rows]} using scripts/enrich/quality.py (one SELECT per check; execute(sql) -> list of tuples)."""
    out = {}
    for name, sql in QUALITY_SQL.items():
        rows = execute(sql)
        if name == "description_style":
            rows = [r for r in rows if quality.check_description_style(r[1])]
        elif name == "tag_label_ja_not_japanese":
            rows = [r for r in rows if not quality.has_japanese(r[1] or "")]
        out[name] = rows
    return out


def wrangler_execute(db, target, persist_to):
    def run(sql):
        cmd = [shutil.which("npx") or "npx", "wrangler", "d1", "execute", db, target, "--json", "--command", sql]
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
    for name, rows in quality_checks(execute).items():
        base = QUALITY_BASELINE[name]
        mark = "ok  " if not rows else "warn"
        trend = "" if len(rows) <= base else f" ABOVE BASELINE {base} (+{len(rows) - base})"
        print(f"[{mark}] quality:{name}: {len(rows)} row(s) (baseline {base}) -- {QUALITY_WHY[name]}{trend}")
        for r in rows[:a.limit]:
            print(f"        {r}")
    print(f"{failed} check(s) failed" if failed else "all error-level checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
