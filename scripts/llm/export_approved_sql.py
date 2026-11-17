#!/usr/bin/env python3
"""Write the approved LLM judgements of the frontier ledger as an idempotent SQL file for the sites tables. Issue #48.

  uv run python3 scripts/llm/export_approved_sql.py [--db var/frontier.sqlite] [--out var/approved-sites.sql]

Read-only on the ledger (SQLite read-only mode); no network, no API. The SQL is a derived file under var/ (git-ignored):
it only INSERTs, and every statement guards itself against what the target database already holds, so running it twice
adds nothing. Apply it to the local D1 only (npx wrangler d1 execute mise --local --file var/approved-sites.sql).

Exported: review_state=approved with verdict pass / suspect and both descriptions. Per site:
  sites (id, url, name, description_ja/en, country_code, region_code) / site_categories / site_tags.
  - id = fr-<host with unsafe characters as '-'>-<8 hex of sha1(key)>; name = the page title (80 chars), or the host when
    the title is empty or the site is adult=yes (the descriptions are already neutral, the title may not be).
  - Skipped by the SQL itself when: the site is already listed (same host+path, www./scheme/trailing slash ignored), or
    the host (or a parent domain) is in blocked_hosts, or the id already exists. Skipped here: duplicates inside the
    approved set (same normalized URL).
  - categories keep only ids that exist in categories; tags (English keywords from the LLM) go to label_ja and label_en.
  - long descriptions are not exported (no column for them yet). Existing rows are never updated.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COUNTRY = re.compile(r"[A-Z]{2}")
REGION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")


def q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def norm_url(u):
    """Same comparison key as norm_sql(): lower case, no scheme, no www., no trailing slash."""
    return re.sub(r"^https?://", "", u.strip().lower()).replace("www.", "").rstrip("/")


def norm_sql(col):
    return f"rtrim(replace(replace(replace(lower({col}),'https://',''),'http://',''),'www.',''),'/')"


def site_id(key):
    safe = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40]
    return f"fr-{safe}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def site_name(row):
    title = re.sub(r"\s+", " ", row["title"] or "").strip()
    if row["adult"] == "yes" or not title:
        return row["host"]
    return title[:80]


def blocked_sql(url):
    sp = urllib.parse.urlsplit(url)
    host = (sp.hostname or "").rstrip(".").lower()
    host = host[4:] if host.startswith("www.") else host
    path = sp.path.rstrip("/").lower()
    return ("EXISTS (SELECT 1 FROM blocked_hosts b WHERE (b.path_prefix='' AND (b.host=%s OR %s LIKE '%%.' || b.host)) "
            "OR (b.path_prefix<>'' AND b.host=%s AND (%s=b.path_prefix OR %s LIKE b.path_prefix || '/%%')))"
            % (q(host), q(host), q(host), q(path), q(path)))


def statements(row):
    sid, url = site_id(row["key"]), row["final_url"] or row["url"]
    cc, rc = row["country_code"], row["region_code"]
    cc = cc if cc and COUNTRY.fullmatch(cc) else None
    rc = rc if cc and rc and REGION.fullmatch(rc) and rc.startswith(cc + "-") else None
    out = [
        f"INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code) "
        f"SELECT {q(sid)},{q(url)},{q(site_name(row))},{q(row['description_ja'])},{q(row['description_en'])},{q(cc)},{q(rc)} "
        f"WHERE NOT EXISTS (SELECT 1 FROM sites WHERE {norm_sql('url')}={q(norm_url(url))}) AND NOT {blocked_sql(url)} "
        f"ON CONFLICT DO NOTHING;"]
    for cat in json.loads(row["categories"]):
        out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT {q(sid)},{q(cat)} "
                   f"WHERE EXISTS (SELECT 1 FROM sites WHERE id={q(sid)}) AND EXISTS (SELECT 1 FROM categories WHERE id={q(cat)});")
    for tag in json.loads(row["tags"]):
        out.append(f"INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) SELECT {q(sid)},{q(tag)},{q(tag)} "
                   f"WHERE EXISTS (SELECT 1 FROM sites WHERE id={q(sid)});")
    return out


def build(db_path):
    """Returns (sql text, stats)."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT j.*, f.host, f.url, f.final_url, f.title FROM llm_judgement j JOIN frontier f ON f.key=j.key "
            "WHERE j.review_state='approved' ORDER BY j.key").fetchall()
    finally:
        con.close()
    stats = {"approved": len(rows), "exported": 0, "skipped_verdict": 0, "skipped_no_description": 0, "skipped_duplicate": 0}
    seen, body = set(), []
    for r in rows:
        if r["spam_verdict"] not in ("pass", "suspect"):
            stats["skipped_verdict"] += 1
        elif not (r["description_ja"] and r["description_en"]):
            stats["skipped_no_description"] += 1
        elif norm_url(r["final_url"] or r["url"]) in seen:
            stats["skipped_duplicate"] += 1
        else:
            seen.add(norm_url(r["final_url"] or r["url"]))
            body.append(f"-- {r['key']}")
            body += statements(r)
            stats["exported"] += 1
    head = ["-- Approved sites from the frontier ledger (scripts/llm/export_approved_sql.py). Derived file: do not edit.",
            "-- Idempotent: every INSERT is guarded against rows that already exist and against blocked_hosts.",
            f"-- {stats['exported']} site(s)."]
    return "\n".join(head + body) + "\n", stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "var", "frontier.sqlite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "var", "approved-sites.sql"))
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"no ledger at {a.db}", file=sys.stderr)
        return 1
    try:
        sql, stats = build(a.db)
    except sqlite3.OperationalError as e:
        print(f"cannot read judgements from {a.db}: {e} (import results first: scripts/llm/import_results.py)", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(sql)
    print(", ".join(f"{k}={v}" for k, v in stats.items()) + f" -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
