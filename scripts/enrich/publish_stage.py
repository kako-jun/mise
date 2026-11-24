"""Stage C "publish" of the online enrichment pipeline: approved url_candidates -> published sites, directly in production D1.

    from publish_stage import run
    run(client, limit=50, apply=False)      # client = scripts/media_online.py Cloudflare (or any object with .query / .batch)

Replaces the SQL-file path (export_approved_sql.py -> gate -> wrangler execute). No local files are read or written.
For every url_progress row with state='approved' (joined to url_candidates) ONE atomic client.batch does:
  1. INSERT sites (guarded: not already listed, not blocked, not rejected by a request, id free; ON CONFLICT DO NOTHING)
  2. INSERT OR IGNORE site_categories   (only when 1 inserted: changes()>0; max 3, canonical form, ids that exist)
  3. INSERT OR IGNORE site_tags         (only when 2 inserted; max 5)
  4. INSERT OR IGNORE site_media_jobs   (favicon + thumbnail; the sites triggers 0033/0035 already create them, this is a no-op safety net)
  5. UPDATE url_progress SET state='published' (only when the site row exists)
Counters (category_counts, scope_counts, closure) and search_text / FTS are maintained by the existing triggers (0018/0020/0022/0026/0027/0030),
so they move in the same transaction. Registered time comes from trigger 0006 (registered_at); bulk_import stays at its default 0.
A site that is already listed is never altered: only url_progress is set to 'published'.
A row failing a gate goes back to state='candidate' with url_candidates.review_reason set (never dropped silently).
After the writes the touched ids are checked with the same SQL semantics as scripts/llm/integrity.py (restricted to those ids).
"""
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in ("scripts/requests", "scripts/llm"):
    sys.path.insert(0, os.path.join(ROOT, _p))
import import_requests  # noqa: E402  (key_of / match_path / block_candidates: the same keys as the intake)
import judge  # noqa: E402  (category tree, old->new ids, canonical form)

MAX_PARAMS = 100
VALID_CATEGORIES = set(judge.category_ids(judge.load_categories()))
LEGACY_CATEGORIES = judge.load_legacy_map()
COUNTRY = re.compile(r"[A-Z]{2}")
REGION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")
OK_VERDICTS = ("pass", "suspect")


def norm_url(u):
    u = re.sub(r"^https?://", "", u.strip().lower())
    u = u[4:] if u.startswith("www.") else u
    return u.replace("%7e", "~").rstrip("/")


def norm_sql(col):
    """SQL twin of norm_url (same text as integrity.norm_sql)."""
    s = (f"CASE WHEN lower(trim({col})) LIKE 'https://%' THEN substr(lower(trim({col})),9) "
         f"WHEN lower(trim({col})) LIKE 'http://%' THEN substr(lower(trim({col})),8) ELSE lower(trim({col})) END")
    return f"rtrim(replace(CASE WHEN ({s}) LIKE 'www.%' THEN substr(({s}),5) ELSE ({s}) END,'%7e','~'),'/')"


def site_id(key):
    safe = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40]
    return f"fr-{safe}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def site_name(row, host):
    title = re.sub(r"\s+", " ", row.get("title") or "").strip()
    if row.get("adult") == "yes" or not title:
        return host
    return title[:80]


def adult_value(row):
    return 1 if row.get("adult") == "yes" else 0 if row.get("adult") == "no" else None


def crawled_at(row):
    v = row.get("last_fetched_at")
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_tags(tags):
    out, seen = [], set()
    for t in tags:
        if not isinstance(t, str):
            continue
        t = re.sub(r"\s+", " ", t).strip()
        if t and len(t) <= 40 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:5]


def _json_list(text):
    try:
        v = json.loads(text) if text else []
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, list) else None


def url_keys(urls):
    return sorted({k for u in urls for k in (import_requests.key_of(u), norm_url(u))})


class Gate(Exception):
    """A pre-publish gate failed: the row goes back to 'candidate' with this reason."""


def plan(row):
    """Validate one joined row and build everything the batch needs. Raises Gate(reason)."""
    key = row.get("frontier_key") or row["identity_key"]
    url = (row.get("final_url") or row.get("url") or "").strip()
    if not re.match(r"https?://", url, re.I):
        raise Gate("gate:no_url")
    host = (row.get("host") or urllib.parse.urlsplit(url).hostname or "").strip().lower()
    if not host:
        raise Gate("gate:no_host")
    if row.get("spam_verdict") not in OK_VERDICTS:
        raise Gate(f"gate:verdict_{row.get('spam_verdict')}")
    dja, den = (row.get("description_ja") or "").strip(), (row.get("description_en") or "").strip()
    if not dja or not den:
        raise Gate("gate:no_description")
    raw_cats = _json_list(row.get("categories"))
    if raw_cats is None:
        raise Gate("gate:bad_categories_json")
    cats = judge.modernize_categories([c for c in raw_cats if isinstance(c, str)], VALID_CATEGORIES, LEGACY_CATEGORIES)
    if not cats:
        raise Gate("gate:no_valid_category")
    raw_tags = _json_list(row.get("tags"))
    if raw_tags is None:
        raise Gate("gate:bad_tags_json")
    name = site_name(row, host)
    if not name.strip():
        raise Gate("gate:empty_name")
    cc, rc = row.get("country_code"), row.get("region_code")
    cc = cc if cc and COUNTRY.fullmatch(cc) else None
    rc = rc if cc and rc and REGION.fullmatch(rc) and rc.startswith(cc + "-") else None
    cands = list(dict.fromkeys([url, (row.get("url") or "").strip() or url, "https://" + key]))
    return {"identity_key": row["identity_key"], "id": site_id(key), "url": url, "name": name, "dja": dja, "den": den, "cc": cc, "rc": rc,
            "adult": adult_value(row), "crawled": crawled_at(row), "cats": cats, "tags": clean_tags(raw_tags), "cands": cands,
            "keys": url_keys(cands)}


def blocked_sql(urls, params):
    parts = []
    for url in urls:
        sp = urllib.parse.urlsplit(url)
        host = (sp.hostname or "").rstrip(".").lower()
        host = host[4:] if host.startswith("www.") else host
        path = import_requests.match_path(sp.path)
        cands = import_requests.block_candidates(host) or [host]
        params.extend(cands)
        a = ",".join("?" * len(cands))
        params.extend([host, path, path])
        parts.append(f"(b.path_prefix='' AND b.host IN ({a})) OR (b.path_prefix<>'' AND b.host=? AND (?=b.path_prefix OR substr(?,1,length(b.path_prefix)+1)=b.path_prefix||'/'))")
    return "EXISTS (SELECT 1 FROM blocked_hosts b WHERE %s)" % " OR ".join("(%s)" % x for x in parts)


def requested_off_sql(keys, params):
    params.extend(keys)
    return (f"EXISTS (SELECT 1 FROM requests r WHERE r.normalized_key IN ({','.join('?' * len(keys))}) "
            "AND ((r.type='add' AND r.status='rejected') OR (r.type='remove' AND r.status='approved')))")


def publish_statements(p):
    """-> [(sql, params)] of the atomic batch for a NEW site."""
    kin = ",".join("?" * len(p["keys"]))
    params = [p["id"], p["url"], p["name"], p["dja"], p["den"], p["cc"], p["rc"], p["adult"], p["crawled"]] + p["keys"]
    bl = blocked_sql(p["cands"], params)
    ro = requested_off_sql(p["keys"], params)
    params.append(p["id"])
    out = [(f"INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code,adult_content,last_crawled_at) "
            f"SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM sites WHERE {norm_sql('url')} IN ({kin})) AND NOT {bl} AND NOT {ro} "
            f"AND NOT EXISTS (SELECT 1 FROM sites WHERE id=?) ON CONFLICT DO NOTHING", params)]
    sel_c = " UNION ALL ".join("SELECT ? AS site_id,? AS category_id" if i == 0 else "SELECT ?,?" for i in range(len(p["cats"])))
    out.append((f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT * FROM ({sel_c}) "
                "WHERE changes()>0 AND category_id IN (SELECT id FROM categories)", [x for c in p["cats"] for x in (p["id"], c)]))
    if p["tags"]:
        sel_t = " UNION ALL ".join("SELECT ?,?,?" for _ in p["tags"])
        out.append((f"INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) SELECT * FROM ({sel_t}) WHERE changes()>0",
                    [x for t in p["tags"] for x in (p["id"], t, t)]))
    return out + media_and_state(p)


def media_and_state(p, new_site=True):
    out = []
    if new_site:
        # the sites triggers (0033/0035) already create these; this OR IGNORE copy is a safety net with the same rules
        # (adult -> thumbnail excluded and eligible=0)
        out.append(("INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible) "
                    "SELECT progress_key,id,'favicon','pending',NULL,1 FROM sites WHERE id=? AND progress_key IS NOT NULL AND favicon_path IS NULL", [p["id"]]))
        out.append(("INSERT OR IGNORE INTO site_media_jobs(identity_key,site_id,kind,state,asset_key,eligible) "
                    "SELECT progress_key,id,'thumbnail',CASE WHEN adult_content=1 THEN 'excluded' ELSE 'pending' END,NULL,CASE WHEN adult_content=1 THEN 0 ELSE 1 END "
                    "FROM sites WHERE id=? AND progress_key IS NOT NULL AND thumb_path IS NULL", [p["id"]]))
    kin = ",".join("?" * len(p["keys"]))
    out.append((f"UPDATE url_progress SET state='published',updated_at=datetime('now') WHERE identity_key=? AND state='approved' "
                f"AND EXISTS (SELECT 1 FROM sites WHERE id=? OR {norm_sql('url')} IN ({kin}))", [p["identity_key"], p["id"]] + p["keys"]))
    return out


def hold_statements(identity_key, reason):
    """Back to 'candidate' with a reason; review_state goes to 'pending' so the approve step does not re-approve it unseen."""
    return [("UPDATE url_progress SET state='candidate',updated_at=datetime('now') WHERE identity_key=? AND state='approved'", [identity_key]),
            ("UPDATE url_candidates SET review_reason=?,review_state='pending',updated_at=datetime('now') WHERE identity_key=?", [reason, identity_key])]


def existing_site(client, p):
    kin = ",".join("?" * len(p["keys"]))
    rows = client.query(f"SELECT id FROM sites WHERE id=? OR {norm_sql('url')} IN ({kin}) LIMIT 1", [p["id"]] + p["keys"])
    return rows[0]["id"] if rows else None


def verify(client, p):
    """Read back one site: -> list of problems (empty = consistent)."""
    r = client.query("SELECT s.id,s.progress_key,"
                     "(SELECT COUNT(*) FROM site_categories WHERE site_id=s.id) nc,(SELECT COUNT(*) FROM site_tags WHERE site_id=s.id) nt,"
                     "(SELECT COUNT(*) FROM site_media_jobs WHERE site_id=s.id AND identity_key=s.progress_key AND kind IN ('favicon','thumbnail')) nj,"
                     "(SELECT state FROM site_media_jobs WHERE site_id=s.id AND kind='thumbnail') thumb_state,"
                     "(SELECT eligible FROM site_media_jobs WHERE site_id=s.id AND kind='thumbnail') thumb_eligible,"
                     "s.adult_content ad FROM sites s WHERE s.id=?", [p["id"]])
    if not r:
        return ["site row missing"]
    r, problems = r[0], []
    if r["nc"] != len(p["cats"]):
        problems.append(f"categories {r['nc']}!={len(p['cats'])}")
    if r["nt"] != len(p["tags"]):
        problems.append(f"tags {r['nt']}!={len(p['tags'])}")
    if r["nj"] != 2:
        problems.append(f"media jobs {r['nj']}!=2")
    if r["ad"] == 1 and (r["thumb_state"] != "excluded" or r["thumb_eligible"] != 0):
        problems.append("adult thumbnail not excluded")
    st = client.query("SELECT state FROM url_progress WHERE identity_key=?", [p["identity_key"]])
    if not st or st[0]["state"] != "published":
        problems.append("url_progress not published")
    return problems


# --- post-check: scripts/llm/integrity.py semantics (error level) restricted to the touched ids ----------------------
def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


COUNT_SQL = ("SELECT cl.ancestor_id,{scope} scope,COUNT(DISTINCT s.id) expected,"
             "COALESCE((SELECT n FROM category_counts WHERE category_id=cl.ancestor_id AND scope={scope}),0) cached "
             "FROM category_closure cl JOIN site_categories x ON x.category_id=cl.descendant_id JOIN sites s ON s.id=x.site_id "
             "WHERE cl.ancestor_id IN (SELECT category_id FROM site_categories WHERE site_id IN ({a})) {where} "
             "GROUP BY cl.ancestor_id,{scope} HAVING expected<>cached")


def post_check(client, ids):
    """-> {check name: [rows]} of violations only (empty dict = fine). Names follow integrity.CHECKS."""
    found = {}

    def add(name, rows):
        if rows:
            found.setdefault(name, []).extend(tuple(r.values()) for r in rows)

    for chunk in _chunks(sorted(set(ids)), 50):
        a = ",".join("?" * len(chunk))
        add("uncategorized", client.query(f"SELECT s.id FROM sites s WHERE s.id IN ({a}) AND NOT EXISTS (SELECT 1 FROM site_categories x WHERE x.site_id=s.id)", chunk))
        add("orphan_site_categories", client.query(f"SELECT x.site_id,x.category_id FROM site_categories x WHERE x.site_id IN ({a}) "
                                                   "AND NOT EXISTS (SELECT 1 FROM categories c WHERE c.id=x.category_id)", chunk))
        add("ancestor_duplicate", client.query("SELECT a.site_id,a.category_id,b.category_id FROM site_categories a JOIN category_closure cl ON cl.ancestor_id=a.category_id "
                                               "AND cl.descendant_id<>a.category_id JOIN site_categories b ON b.site_id=a.site_id AND b.category_id=cl.descendant_id "
                                               f"WHERE a.site_id IN ({a}) AND a.site_id NOT LIKE 'pb-%'", chunk))
        add("empty_text", client.query(f"SELECT id FROM sites WHERE id IN ({a}) AND (trim(name)='' OR trim(description_ja)='' OR trim(name_ja)='' OR trim(name_en)='')", chunk))
        add("duplicate_url", client.query(f"SELECT k,COUNT(*) n FROM (SELECT {norm_sql('url')} AS k FROM sites) WHERE k IN "
                                          f"(SELECT {norm_sql('url')} FROM sites WHERE id IN ({a})) GROUP BY k HAVING COUNT(*)>1", chunk))
        # counters: the triggers must have moved them exactly (scope all / country / region, via category_closure)
        for name, scope, where in (("category_counts_all", "'all'", ""), ("category_counts_country", "s.country_code", "AND s.country_code IS NOT NULL"),
                                   ("category_counts_region", "s.region_code", "AND s.region_code IS NOT NULL")):
            add(name, client.query(COUNT_SQL.format(scope=scope, where=where, a=a), chunk))
    cnt = client.query("SELECT (SELECT n FROM scope_counts WHERE scope='all') cached,(SELECT COUNT(*) FROM sites) expected", [])
    if cnt and cnt[0]["cached"] != cnt[0]["expected"]:
        found["scope_counts_all"] = [(cnt[0]["cached"], cnt[0]["expected"])]
    return found


# --- runner -------------------------------------------------------------------------------------------------------------
def run(client, *, limit, apply, log=print):
    rows = client.query(
        "SELECT p.identity_key,c.* FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key "
        "WHERE p.state='approved' ORDER BY p.identity_key LIMIT ?", [int(limit)])
    res = {"selected": len(rows), "published": 0, "already_listed": 0, "held": 0, "errors": 0, "dry_run": not apply,
           "would_publish": 0, "held_reasons": {}, "verify_problems": {}, "integrity": {}, "ids": []}
    touched = []

    def hold(identity_key, reason):
        res["held"] += 1
        res["held_reasons"][reason] = res["held_reasons"].get(reason, 0) + 1
        log(f"hold {identity_key}: {reason}")
        if apply:
            client.batch(hold_statements(identity_key, reason))

    for row in rows:
        try:
            p = plan(dict(row))
        except Gate as g:
            hold(row["identity_key"], str(g))
            continue
        try:
            stmts = publish_statements(p)
            if max(len(s[1]) for s in stmts) > MAX_PARAMS:
                raise Gate("gate:too_many_params")
            if existing_site(client, p):
                res["already_listed"] += 1
                if apply:
                    client.batch(media_and_state(p, new_site=False))   # the existing sites row is never altered
                continue
            if not apply:
                res["would_publish"] += 1
                continue
            client.batch(stmts)
        except Gate as g:
            hold(p["identity_key"], str(g))
            continue
        except Exception as e:   # the batch is atomic: nothing of this site was written; the row stays 'approved' and is retried next run
            res["errors"] += 1
            log(f"error {p['identity_key']}: {e}")
            continue
        problems = verify(client, p)
        if problems:
            st = client.query("SELECT state FROM url_progress WHERE identity_key=?", [p["identity_key"]])
            if not client.query("SELECT 1 FROM sites WHERE id=?", [p["id"]]) and st and st[0]["state"] == "approved":
                hold(p["identity_key"], "gate:not_inserted_blocked_or_rejected_request")   # skipped by a SQL guard (blocked host / request / race)
            else:
                res["verify_problems"][p["id"]] = problems
                log(f"verify {p['id']}: {problems}")
            continue
        res["published"] += 1
        res["ids"].append(p["id"])
        touched.append(p["id"])
    if apply and touched:
        res["integrity"] = post_check(client, touched)
        for n, r in res["integrity"].items():
            log(f"integrity {n}: {r[:3]}")
    log("publish_stage: " + json.dumps({k: v for k, v in res.items() if k != "ids"}, ensure_ascii=False))
    return res
