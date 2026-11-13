#!/usr/bin/env python3
"""Compare pipeline.py runs (arms) in one table: tokens, cost, retries, and agreement with a baseline arm.

Usage: exp_summary.py <baseline.json> <arm.json> [<arm.json> ...]
Agreement is measured against the first file: same verdict, same site_kind, overlapping category ids,
adult attribute, and (for the described subset) whether a long description exists.
"""
import json
import sys


def load(p):
    d = json.load(open(p, encoding="utf-8"))
    return d, {r["id"]: r for r in d["results"]}


def verdict(r):
    o = r.get("output") or r.get("triage")
    return o["spam"]["verdict"] if o else None


def main(base_path, *paths):
    base, bres = load(base_path)
    print("| arm | items | requests | in | out | cache_write | cache_read | retries | unresolved | USD | USD/item | wall s | verdict agree | kind agree | category overlap | long desc |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for path in [base_path, *paths]:
        d, res = load(path)
        st = d["stats"].values()
        tot = {k: sum(s[k] for s in st) for k in ("requests", "in", "out", "cw", "cr", "usd", "retry_items")}
        n = len(res)
        va = ka = ca = lo = 0
        for cid, r in res.items():
            b = bres.get(cid, {})
            o, bo = r.get("output"), b.get("output")
            va += verdict(r) == verdict(b) and verdict(r) is not None
            if o and bo:
                ka += o["site_kind"] == bo["site_kind"]
                ca += bool(set(o["categories"]) & set(bo["categories"])) or (not o["categories"] and not bo["categories"])
            lo += bool(o and o.get("long_description_ja"))
        name = path.rsplit("/", 1)[-1].removesuffix(".json")
        print(f"| {name} | {n} | {tot['requests']} | {tot['in']} | {tot['out']} | {tot['cw']} | {tot['cr']} | {tot['retry_items']} | "
              f"{len(d['failures'])} | {tot['usd']:.4f} | {tot['usd'] / n:.5f} | {d['wall_seconds']:.0f} | {va}/{n} | {ka}/{n} | {ca}/{n} | {lo} |")


if __name__ == "__main__":
    main(*sys.argv[1:])
