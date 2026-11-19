#!/usr/bin/env python3
"""Merge cost ledgers into the shared one (Issue #48).

Before the ledger was fixed to ~/.config/mise/llm-ledger.jsonl, every --var-dir had its own
<var-dir>/llm-ledger.jsonl, so the total budget was split. This appends the records of the old
ledgers to the shared one. Safe to repeat: a record already present (same line, counted as a
multiset) is not added again. Records are written in timestamp order under the ledger's file lock.

Usage: merge_ledgers.py [--into PATH] [--dry-run] old1.jsonl [old2.jsonl ...]
       merge_ledgers.py $(find var -name llm-ledger.jsonl)
"""
import argparse
import collections
import json
import os
import sys

import judge


def lines_of(path):
    led = judge.Ledger(path, 0, 0)
    return [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in led._lines()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--into", default=judge.DEFAULT_LEDGER)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    target = judge.Ledger(a.into, 0, 0)
    with target._lock():
        have = collections.Counter(lines_of(a.into)) if os.path.exists(a.into) else collections.Counter()
        add = []
        for src in a.sources:
            if os.path.abspath(src) == os.path.abspath(a.into):
                continue
            want = collections.Counter(lines_of(src))
            for line, n in want.items():
                add += [line] * max(0, n - have[line])
            have.update({k: max(0, want[k] - have[k]) for k in want})
        add.sort(key=lambda l: json.loads(l).get("ts", ""))
        if not a.dry_run and add:
            with open(a.into, "a", encoding="utf-8") as f:
                for line in add:
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
    added = sum(r["cost_usd"] for r in map(json.loads, add) if r["kind"] == "call")
    msg = f"{'would add' if a.dry_run else 'added'} {len(add)} record(s) (calls worth ${added:.4f}) from {len(a.sources)} ledger(s)"
    if not a.dry_run:
        msg += f"; {a.into} total now ${target.total():.4f}"
    print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
