#!/usr/bin/env python3
"""Build judge.py input from the already-listed sites (no network access).

The schema is created in memory by applying migrations/*.sql, so this works without
wrangler or a local D1 file. Fetched text (title / meta description) comes from the
collection JSON files in data/. Human-written descriptions, categories and countries
are NOT put into the candidate (they would leak the answer); they go to a separate
reference file used for comparison.
"""
import argparse
import glob
import json
import pathlib
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_db(root=ROOT):
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);")
    for f in sorted(glob.glob(os.path.join(root, "migrations", "*.sql"))):
        c.executescript(pathlib.Path(f).read_text(encoding="utf-8"))
    return c


def fetched_text(root=ROOT):
    """url -> {title, meta_description} from the stored collection results."""
    by_url = {}
    p = os.path.join(root, "data", "collection-sample.json")
    for r in json.loads(pathlib.Path(p).read_text(encoding="utf-8")):
        by_url[r["url"]] = {"title": r.get("title"), "meta_description": r.get("meta_description"),
                            "http_status": r.get("page_http_status"), "checked_at": r.get("checked_at")}
    p = os.path.join(root, "data", "collection-batch1.json")
    for r in json.loads(pathlib.Path(p).read_text(encoding="utf-8")):
        by_url[r["url"]] = {"title": r.get("observed_title"), "meta_description": r.get("observed_meta_description"),
                            "http_status": r.get("page_http_status"), "checked_at": r.get("checked_at")}
    return by_url


def export(root=ROOT, with_human_description=False):
    db = load_db(root)
    fetched = fetched_text(root)
    cands, refs = [], []
    for sid, url, name, dja, den, cc, rc in db.execute(
            "SELECT id,url,name,description_ja,description_en,country_code,region_code FROM sites ORDER BY id"):
        cats = [r[0] for r in db.execute(
            "SELECT category_id FROM site_categories WHERE site_id=? ORDER BY category_id", (sid,))]
        f = fetched.get(url, {})
        meta = f.get("meta_description")
        features = {"source": "stored fetch result" if f else "no stored fetch (name only)"}
        if f.get("http_status"):
            features["http_status"] = f["http_status"]
        if not meta and with_human_description:
            meta = dja
            features["meta_description_is"] = "existing human-written description (no fetched text)"
        cands.append({"id": sid, "url": url, "title": f.get("title") or name, "meta_description": meta,
                      "lang": None, "body_excerpt": "", "features": features})
        refs.append({"id": sid, "name": name, "description_ja": dja, "description_en": den,
                     "categories": cats, "country_code": cc, "region_code": rc})
    db.close()
    return cands, refs


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default=os.path.join(ROOT, "var"))
    p.add_argument("--with-human-description", action="store_true",
                   help="use the existing description as meta_description when no fetched text exists")
    a = p.parse_args(argv)
    cands, refs = export(ROOT, a.with_human_description)
    os.makedirs(a.out_dir, exist_ok=True)
    cp = os.path.join(a.out_dir, "sites-candidates.jsonl")
    with open(cp, "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    rp = os.path.join(a.out_dir, "sites-reference.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(refs, f, ensure_ascii=False, indent=1)
    print(f"{len(cands)} sites -> {cp}; reference -> {rp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
