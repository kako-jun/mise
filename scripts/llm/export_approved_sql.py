#!/usr/bin/env python3
"""Write the approved LLM judgements of the frontier ledger as an idempotent SQL file for the sites tables. Issue #48.

  uv run python3 scripts/llm/export_approved_sql.py [--db var/frontier.sqlite] [--out var/approved-sites.sql]

Read-only on the ledger (SQLite read-only mode); no network, no API. The SQL is a derived file under var/ (git-ignored):
it only INSERTs, and every statement guards itself against what the target database already holds, so running it twice
adds nothing. Apply it to the local D1 only (npx wrangler d1 execute mise --local --file var/approved-sites.sql).

Exported: review_state=approved with verdict pass / suspect and both descriptions, and only while the host is still
frontier.state='candidate' (a host that moved to rejected / blocked_by_robots / deferred is not exported even if approved).
Per site: sites (id, url, name, description_ja/en, country_code, region_code) / site_categories (max 3) / site_tags (max 5).
  - id = fr-<host with unsafe characters as '-'>-<8 hex of sha1(key)>; name = the frontier title as of the export (it can
    differ from the title the LLM saw), or the host when the title is empty or the site is adult=yes.
  - Skipped by the SQL itself when: the site is already listed (same host+path; scheme, a leading www., trailing slash, case
    and %7E/~ ignored), the host (or a parent domain, with the shared-hosting rule of the intake) is in blocked_hosts, a
    path_prefix row of blocked_hosts covers the path (same path folding as src/intake.js), a request of the same
    normalized_key was rejected (type add) or approved as a removal (type remove), or the id already exists.
    Skipped here: duplicates inside the approved set (same normalized URL).
  - Children (tags, then categories) are inserted only when the sites INSERT right before inserted a row (changes()), so
    re-approving and re-exporting a site that is already listed never adds categories or tags to it.
  - categories keep only ids that exist in categories; tags (English keywords from the LLM) go to label_ja and label_en
    (case-insensitive duplicates and tags over 40 chars are dropped).
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
sys.path.insert(0, os.path.join(ROOT, "scripts", "requests"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "pipeline"))
import import_requests  # noqa: E402  (key_of / match_path / block_candidates: the same keys as the intake and the request import)

COUNTRY = re.compile(r"[A-Z]{2}")
REGION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")


def q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def norm_url(u):
    """Comparison key mirroring norm_sql(): lower case, no scheme, no leading www., %7e as ~, no trailing slash."""
    u = re.sub(r"^https?://", "", u.strip().lower())
    u = u[4:] if u.startswith("www.") else u
    return u.replace("%7e", "~").rstrip("/")


def norm_sql(col):
    """SQL twin of norm_url (only the scheme and a leading www. are removed; migration 0015 removed any 'www.')."""
    s = f"CASE WHEN lower(trim({col})) LIKE 'https://%' THEN substr(lower(trim({col})),9) " \
        f"WHEN lower(trim({col})) LIKE 'http://%' THEN substr(lower(trim({col})),8) ELSE lower(trim({col})) END"
    return f"rtrim(replace(CASE WHEN ({s}) LIKE 'www.%' THEN substr(({s}),5) ELSE ({s}) END,'%7e','~'),'/')"


def as_list(urls):
    return [urls] if isinstance(urls, str) else list(urls)


def url_keys(urls):
    """Both comparison forms (the request key and the plain SQL-side key) of every candidate URL."""
    return sorted({k for u in as_list(urls) for k in (import_requests.key_of(u), norm_url(u))})


def site_id(key):
    safe = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40]
    return f"fr-{safe}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def site_name(row):
    title = re.sub(r"\s+", " ", row["title"] or "").strip()
    if row["adult"] == "yes" or not title:
        return row["host"]
    return title[:80]


def blocked_sql(urls):
    """Same rule as the intake: the host or a parent domain (never above a shared-hosting user area) for whole-host rows,
    the host itself plus the folded path for path_prefix rows. No LIKE, so _ and % in a row are plain characters.
    Several candidate URLs (url / final_url / key of one site): blocked when any of them is."""
    parts = []
    for url in as_list(urls):
        sp = urllib.parse.urlsplit(url)
        host = (sp.hostname or "").rstrip(".").lower()
        host = host[4:] if host.startswith("www.") else host
        path = import_requests.match_path(sp.path)
        cands = ",".join(q(h) for h in import_requests.block_candidates(host)) or q(host)
        parts.append("(b.path_prefix='' AND b.host IN (%s)) "
                     "OR (b.path_prefix<>'' AND b.host=%s AND (%s=b.path_prefix OR substr(%s,1,length(b.path_prefix)+1)=b.path_prefix||'/'))"
                     % (cands, q(host), q(path), q(path)))
    return "EXISTS (SELECT 1 FROM blocked_hosts b WHERE %s)" % " OR ".join("(%s)" % x for x in parts)


def requested_off_sql(urls):
    """A rejected recommendation (manual / withdrawn / expired / blocklisted) or an approved removal for the same key."""
    keys = ",".join(q(k) for k in url_keys(urls))
    return (f"EXISTS (SELECT 1 FROM requests r WHERE r.normalized_key IN ({keys}) "
            "AND ((r.type='add' AND r.status='rejected') OR (r.type='remove' AND r.status='approved')))")


def clean_tags(tags):
    out, seen = [], set()
    for t in tags:
        t = re.sub(r"\s+", " ", t).strip()
        if t and len(t) <= 40 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:5]


def statements(row):
    sid, url = site_id(row["key"]), row["final_url"] or row["url"]
    cc, rc = row["country_code"], row["region_code"]
    cc = cc if cc and COUNTRY.fullmatch(cc) else None
    rc = rc if cc and rc and REGION.fullmatch(rc) and rc.startswith(cc + "-") else None
    # redirects keep the host but may change the path (/ -> /ja/), so every spelling of the site is checked
    cands = list(dict.fromkeys([url, row["url"], "https://" + row["key"]]))
    keys = ",".join(q(k) for k in url_keys(cands))
    out = [
        f"INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code) "
        f"SELECT {q(sid)},{q(url)},{q(site_name(row))},{q(row['description_ja'])},{q(row['description_en'])},{q(cc)},{q(rc)} "
        f"WHERE NOT EXISTS (SELECT 1 FROM sites WHERE {norm_sql('url')} IN ({keys})) AND NOT {blocked_sql(cands)} "
        f"AND NOT {requested_off_sql(cands)} ON CONFLICT DO NOTHING;"]
    # changes() is the row count of the statement right before: 1 after a new sites row, 0 when it was skipped. Tags come first
    # (one statement; a new site gets at least one row), categories second (they see the sites or the tags count). An empty
    # list emits no statement and leaves the previous count in place, so the chain stays correct.
    tags = clean_tags(json.loads(row["tags"]))
    if tags:
        sel = " UNION ALL ".join(f"SELECT {q(sid)},{q(t)},{q(t)}" for t in tags)
        out.append(f"INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) SELECT * FROM ({sel}) WHERE changes()>0;")
    cats = list(dict.fromkeys(json.loads(row["categories"])))[:3]
    if cats:
        sel = " UNION ALL ".join(f"SELECT {q(sid)}{' AS site_id' if i == 0 else ''},{q(c)}{' AS category_id' if i == 0 else ''}"
                                 for i, c in enumerate(cats))
        out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT * FROM ({sel}) "
                   f"WHERE changes()>0 AND category_id IN (SELECT id FROM categories);")
    return out


def build(db_path):
    """Returns (sql text, stats)."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT j.*, f.host, f.url, f.final_url, f.title FROM llm_judgement j JOIN frontier f ON f.key=j.key "
            "WHERE j.review_state='approved' AND f.state='candidate' ORDER BY j.key").fetchall()
    finally:
        con.close()
    stats = {"approved": len(rows), "exported": 0, "skipped_verdict": 0, "skipped_no_description": 0, "skipped_duplicate": 0}
    seen, body = set(), []
    for r in rows:
        if r["spam_verdict"] not in ("pass", "suspect"):
            stats["skipped_verdict"] += 1
        elif not (r["description_ja"] and r["description_en"]):
            stats["skipped_no_description"] += 1
        elif import_requests.key_of(r["final_url"] or r["url"]) in seen:
            stats["skipped_duplicate"] += 1
        else:
            seen.add(import_requests.key_of(r["final_url"] or r["url"]))
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
