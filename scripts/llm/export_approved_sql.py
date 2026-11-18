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
    The already-listed / requests / path_prefix checks look at the union of url, final_url and key.
    Skipped here: duplicates inside the approved set (same normalized URL). That dedup only looks at key_of(final_url or url);
    whatever it misses is absorbed by the SQL-side already-listed check (and by the changes() guard on the child rows).
  - Children (tags, then categories) are inserted only when the sites INSERT right before inserted a row (changes()), so
    re-approving and re-exporting a site that is already listed never adds categories or tags to it.
  - categories are translated from old (pre-#114) ids and keep only ids that exist in categories; tags (English keywords from the LLM) go to label_ja and label_en
    (case-insensitive duplicates and tags over 40 chars are dropped).
  - long descriptions are not exported (no column for them yet). Existing rows are never updated.

Category update mode (Issue #122; a separate mode, never mixed with the INSERT output above): sites that are already listed
keep the categories 0021 moved mechanically, so a re-judgement does not reach them. These options write a different file
(default var/update-categories.sql) that REPLACES site_categories of listed sites, idempotently (DELETE then INSERT per site,
both guarded by "the site exists" and "it has no category under government"):

  --update-categories                  approved ledger rows (frontier.state='candidate', verdict pass / suspect) whose site is
                                       listed under the same id as the export (fr-<host>-<sha1 of the key>)
  --update-categories-from RESULTS --input CANDIDATES.jsonl
                                       pipeline.py / judge.py results whose id is a site id (sample-*, collected-* ... the ids
                                       export_sites.py writes); only verdict pass, output re-validated with judge.validate_output
  --current DB                         a sqlite file of the target (the local D1) to read the present sites / categories; with it the
                                       summary shows real changes and sites that are not listed / have a government leaf are left out
  --dry-run                            print the summary only, write no SQL

Never touched: ids starting with pb-, sites holding a leaf under government (guarded in SQL too), an empty judgement or one
without any valid id (old ids are translated first, see judge.modernize_categories). Summary on stdout.
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
sys.path.insert(0, os.path.join(ROOT, "scripts", "llm"))
import judge  # noqa: E402  (category tree and the old->new id table)
import import_requests  # noqa: E402  (key_of / match_path / block_candidates: the same keys as the intake and the request import)

VALID_CATEGORIES = set(judge.category_ids(judge.load_categories()))
LEGACY_CATEGORIES = judge.load_legacy_map()
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
    # Judgements stored before the taxonomy change (#114) carry old ids; translate them (see judge.modernize_categories).
    cats = judge.modernize_categories(json.loads(row["categories"]), VALID_CATEGORIES, LEGACY_CATEGORIES)
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


# ---------------------------------------------------------------------------------------------------------------------
# Category update mode (#122)
RETREAT = ("business", "society", "business-services", "community")  # where 0021 parked the old company / organisation ids


def read_current(path):
    """-> (sites {id: sorted category ids}, parent {category id: parent id}) from a sqlite file of the target, read-only."""
    con = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro", uri=True)
    try:
        sites = {r[0]: [] for r in con.execute("SELECT id FROM sites")}
        for sid, cid in con.execute("SELECT site_id,category_id FROM site_categories ORDER BY category_id"):
            if sid in sites:
                sites[sid].append(cid)
        parent = {r[0]: r[1] for r in con.execute("SELECT id,parent_id FROM categories")}
    finally:
        con.close()
    return sites, parent


def plan_from_ledger(db_path):
    """-> (items [(site id, [new category ids])], stats). Same row selection as build()."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT j.key, j.spam_verdict, j.categories, f.url, f.final_url FROM llm_judgement j JOIN frontier f ON f.key=j.key "
            "WHERE j.review_state='approved' AND f.state='candidate' ORDER BY j.key").fetchall()
    finally:
        con.close()
    stats = {"approved": len(rows), "skipped_verdict": 0, "skipped_empty": 0}
    items, seen = [], set()
    for r in rows:
        if r["spam_verdict"] not in ("pass", "suspect"):
            stats["skipped_verdict"] += 1
            continue
        cats = judge.modernize_categories(json.loads(r["categories"] or "[]"), VALID_CATEGORIES, LEGACY_CATEGORIES)
        if not cats:
            stats["skipped_empty"] += 1
            continue
        sid = site_id(r["key"])
        if sid not in seen:
            seen.add(sid)
            items.append((sid, cats))
    return items, stats


def plan_from_results(results_path, input_path):
    """-> (items, stats) from a pipeline.py / judge.py results file whose ids are site ids. Only verdict pass counts; the
    output goes through judge.validate_output against the rendered candidate text, like import_results.py."""
    import import_results  # noqa: E402  (local import: it pulls in the ledger module)
    _origin, loaded = import_results.load_results(results_path)
    cands = {c["id"]: c for c in import_results.read_jsonl(input_path)}
    stats = {"results": len(loaded), "no_output": 0, "rejected": 0, "skipped_verdict": 0, "skipped_empty": 0, "skipped_protected": 0}
    problems, items, seen = [], [], set()
    for cid, out, _model, _err, body_limit in loaded:
        if out is None:
            stats["no_output"] += 1
            continue
        if cid.startswith("pb-"):
            stats["skipped_protected"] += 1
            continue
        cand = cands.get(cid)
        if cand is None:
            stats["rejected"] += 1
            problems.append(f"{cid}: candidate not in --input")
            continue
        cats = out.get("categories") if isinstance(out, dict) else None
        if isinstance(cats, list) and any(isinstance(c, str) and c not in VALID_CATEGORIES and c in LEGACY_CATEGORIES for c in cats):
            out = dict(out, categories=judge.modernize_categories(cats, VALID_CATEGORIES, LEGACY_CATEGORIES)
                       + [c for c in cats if not (isinstance(c, str) and (c in VALID_CATEGORIES or c in LEGACY_CATEGORIES))])
        errs = judge.validate_output(out, VALID_CATEGORIES, judge.render_candidate(cand, body_limit))
        if errs:
            stats["rejected"] += 1
            problems.append(f"{cid}: invalid output: " + "; ".join(errs))
            continue
        if out["spam"]["verdict"] != "pass":
            stats["skipped_verdict"] += 1
            continue
        new = judge.modernize_categories(out["categories"], VALID_CATEGORIES, LEGACY_CATEGORIES)
        if not new:
            stats["skipped_empty"] += 1
        elif cid not in seen:
            seen.add(cid)
            items.append((cid, new))
    return items, stats, problems


def update_statements(sid, cats):
    gov = (f"EXISTS (SELECT 1 FROM site_categories sc JOIN categories c ON c.id=sc.category_id "
           f"WHERE sc.site_id={q(sid)} AND c.parent_id='government')")
    listed = f"EXISTS (SELECT 1 FROM sites WHERE id={q(sid)})"
    ids = ",".join(q(c) for c in cats)
    return [f"DELETE FROM site_categories WHERE site_id={q(sid)} AND {listed} AND NOT {gov};",
            f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT {q(sid)},id FROM categories WHERE id IN ({ids}) "
            f"AND {listed} AND NOT EXISTS (SELECT 1 FROM site_categories WHERE site_id={q(sid)});"]


def build_update(items, current=None):
    """-> (sql text, summary dict). current = read_current() result or None (then nothing is known about the target)."""
    sites, parent = current if current else (None, {})
    summary = {"planned": len(items), "sites_changed": 0, "unchanged": 0, "not_listed": 0, "government_leaf": 0,
               "retreat_to_real": 0, "gained": {}, "lost": {}}
    body = []
    for sid, cats in items:
        if sites is not None:
            old = sites.get(sid)
            if old is None:
                summary["not_listed"] += 1
                continue
            if any(parent.get(c) == "government" for c in old):
                summary["government_leaf"] += 1
                continue
            if set(old) == set(cats):
                summary["unchanged"] += 1
                continue
            summary["sites_changed"] += 1
            for c in set(cats) - set(old):
                summary["gained"][c] = summary["gained"].get(c, 0) + 1
            for c in set(old) - set(cats):
                summary["lost"][c] = summary["lost"].get(c, 0) + 1
            if (set(old) & set(RETREAT)) - set(cats) and set(cats) - set(RETREAT):
                summary["retreat_to_real"] += 1
        body.append(f"-- {sid}")
        body += update_statements(sid, cats)
    head = ["-- Category replacement for listed sites (scripts/llm/export_approved_sql.py --update-categories*). Derived file: do not edit.",
            "-- Idempotent: per site, DELETE then INSERT, guarded by 'the site exists' and 'no category under government'.",
            f"-- {len(body) // 3} site(s)." if body else "-- 0 site(s)."]
    return "\n".join(head + body) + "\n", summary


def print_summary(summary, stats, known):
    print("update-categories summary")
    print("  " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    print(f"  planned sites: {summary['planned']}")
    if not known:
        print("  (no --current: changes and unlisted sites are unknown; the SQL guards itself)")
        return
    print(f"  sites changed: {summary['sites_changed']}, unchanged: {summary['unchanged']}, not listed: {summary['not_listed']}, "
          f"skipped (government leaf): {summary['government_leaf']}")
    print(f"  sites moving from a retreat category ({'/'.join(RETREAT)}) to a real one: {summary['retreat_to_real']}")
    for title, d in (("gained", summary["gained"]), ("lost", summary["lost"])):
        print(f"  categories {title} (sites):")
        for c, n in sorted(d.items(), key=lambda x: (-x[1], x[0]))[:40]:
            print(f"    {c}: {n}")


def main_update(a):
    if a.update_categories and a.update_categories_from:
        print("--update-categories and --update-categories-from are separate modes", file=sys.stderr)
        return 1
    problems = []
    try:
        if a.update_categories:
            if not os.path.exists(a.db):
                print(f"no ledger at {a.db}", file=sys.stderr)
                return 1
            items, stats = plan_from_ledger(a.db)
        else:
            if not a.input:
                print("--update-categories-from needs --input (the candidates JSONL given to the judge)", file=sys.stderr)
                return 1
            items, stats, problems = plan_from_results(a.update_categories_from, a.input)
        current = read_current(a.current) if a.current else None
    except sqlite3.OperationalError as e:
        print(f"cannot read: {e}", file=sys.stderr)
        return 1
    for p in problems:
        print("REJECTED " + p, file=sys.stderr)
    sql, summary = build_update(items, current)
    print_summary(summary, stats, current is not None)
    out = a.out if a.out != os.path.join(ROOT, "var", "approved-sites.sql") else os.path.join(ROOT, "var", "update-categories.sql")
    if not a.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(sql)
        print(f"  -> {out}")
    return 2 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "var", "frontier.sqlite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "var", "approved-sites.sql"))
    ap.add_argument("--update-categories", action="store_true", help="category update mode from the approved ledger rows (#122)")
    ap.add_argument("--update-categories-from", metavar="RESULTS", help="category update mode from a results file (site ids; needs --input)")
    ap.add_argument("--input", help="candidates JSONL given to the judge (with --update-categories-from)")
    ap.add_argument("--current", help="sqlite file of the target (local D1) to read present sites / categories for the summary")
    ap.add_argument("--dry-run", action="store_true", help="update mode: print the summary, write no SQL")
    a = ap.parse_args(argv)
    if a.update_categories or a.update_categories_from:
        return main_update(a)
    if a.dry_run or a.current or a.input:
        print("--dry-run / --current / --input belong to the category update mode", file=sys.stderr)
        return 1
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
