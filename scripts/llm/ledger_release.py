#!/usr/bin/env python3
"""List or manually release open reservations on the shared cost ledger (Issue #48).

A reservation stays open when a run died while a request / batch may have been billed (a POST that got no
answer, a crash while polling). Resume the batch by re-running the same command if it exists. Release by hand
only after checking in the Console that nothing was billed or queued; the real cost then has to be added by
hand to the ledger (or accepted as unrecorded).

Usage: ledger_release.py --list
       ledger_release.py TAG [--ledger PATH]
"""
import argparse
import sys

import judge


def open_reservations(ledger):
    net = {}
    with ledger._lock():  # a consistent read while another run may be appending
        rows = ledger._lines()
    for r in rows:
        if r["kind"] == "reserve":
            net[r["batch"]] = net.get(r["batch"], 0.0) + r["usd"]
        elif r["kind"] == "release":
            net[r["batch"]] = net.get(r["batch"], 0.0) - r["usd"]
    return {k: round(v, 6) for k, v in net.items() if v > 1e-9}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--ledger", default=judge.DEFAULT_LEDGER)
    a = ap.parse_args(argv)
    led = judge.Ledger(a.ledger, 0, 0)
    opened = open_reservations(led)
    if a.list or not a.tag:
        for tag, usd in sorted(opened.items()):
            print(f"{tag}\t${usd:.6f}")
        print(f"{len(opened)} open reservation(s), ${sum(opened.values()):.6f}", file=sys.stderr)
        return 0
    if a.tag not in opened:
        print(f"no open reservation {a.tag!r}", file=sys.stderr)
        return 1
    led.release(a.tag)
    print(f"released {a.tag} (${opened[a.tag]:.6f}); ledger total now ${led.total():.6f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
