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
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from progress_identity import progress_identity_key  # noqa: E402
import import_requests  # noqa: E402  (key_of / match_path / block_candidates: the same keys as the intake)
import judge  # noqa: E402  (category tree, old->new ids, canonical form)
sys.path.insert(0, HERE)
import quality  # noqa: E402  (name / description / tags / categories rules; the same functions the judge uses)

MAX_PARAMS = 100
VALID_CATEGORIES = set(judge.category_ids(judge.load_categories()))
LEGACY_CATEGORIES = judge.load_legacy_map()
LEAF_CATEGORIES = quality.leaf_ids_of(list(judge.load_tree()[0].items())) & VALID_CATEGORIES
SYNONYMS = quality.load_synonyms(os.path.join(ROOT, "data", "tag-synonyms.csv"))
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
    """-> (name, repaired). The <title> as the site's name when it passes quality.check_name; else quality.suggest_name (a deterministic
    repair, repaired=True); else None (the caller holds the row: gate:quality_name). Never cut at a fixed length."""
    title = re.sub(r"\s+", " ", row.get("title") or "").strip()
    if row.get("adult") == "yes" or not title:
        return host, False
    if not quality.check_name(title):
        return title, False
    fixed = quality.suggest_name(title, host)
    return (fixed, True) if fixed and not quality.check_name(fixed) else (None, False)


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
    """-> [(label_ja, label_en)] (stored 'ja|en' strings or dicts; see quality.parse_tag). Quality (Japanese label, no near duplicates) is
    checked by plan() with quality.check_tags BEFORE this."""
    out = []
    for t in tags:
        p = quality.parse_tag(t)
        if p and p[0]:
            out.append(p)
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


def gate_reason(row):
    """-> the gate reason ('gate:...') that publishing this joined url_candidates row would hit, or None. The deliberate stage uses it
    so it never approves a row this stage would only send back (same function, not a copy)."""
    try:
        plan(row)
    except Gate as e:
        return str(e)
    return None


def plan(row):
    """Validate one joined row and build everything the batch needs. Raises Gate(reason)."""
    key = row.get("frontier_key") or row["identity_key"]
    url = (row.get("final_url") or row.get("url") or "").strip()
    if not re.match(r"https?://", url, re.I) or re.search(r"[\x00-\x20\x7f]", url):
        raise Gate("gate:no_url")
    try:
        pkey = progress_identity_key(url)   # rejects userinfo / bad authority; the same identity the sites trigger gives
    except ValueError:
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
    name, name_repaired = site_name(row, host)
    if name is None:
        raise Gate("gate:quality_name")
    if not name.strip():
        raise Gate("gate:empty_name")
    desc_repaired = False
    if quality.check_description_style(dja):
        fixed = quality.to_polite(dja)
        if not fixed or quality.check_description_style(fixed):
            raise Gate("gate:quality_description")
        dja, desc_repaired = fixed, True
    if quality.check_tags(raw_tags[:quality.TAG_COUNT_MAX], SYNONYMS):
        raise Gate("gate:quality_tags")
    if quality.check_categories(cats, LEAF_CATEGORIES):
        raise Gate("gate:quality_categories")
    cc, rc = row.get("country_code"), row.get("region_code")
    cc = cc if cc and COUNTRY.fullmatch(cc) else None
    rc = rc if cc and rc and REGION.fullmatch(rc) and rc.startswith(cc + "-") else None
    cands = list(dict.fromkeys([url, (row.get("url") or "").strip() or url, "https://" + key]))
    return {"identity_key": row["identity_key"], "id": site_id(key), "url": url, "name": name, "dja": dja, "den": den, "cc": cc, "rc": rc,
            "adult": adult_value(row), "crawled": crawled_at(row), "cats": cats, "tags": clean_tags(raw_tags), "cands": cands,
            "keys": url_keys(cands), "pkey": pkey, "repaired": {"name": name_repaired, "description": desc_repaired}}


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
    params += [p["id"], p["pkey"]]
    out = [(f"INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code,adult_content,last_crawled_at) "
            f"SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM sites WHERE {norm_sql('url')} IN ({kin})) AND NOT {bl} AND NOT {ro} "
            f"AND NOT EXISTS (SELECT 1 FROM sites WHERE id=?) AND NOT EXISTS (SELECT 1 FROM sites WHERE progress_key=?) ON CONFLICT DO NOTHING", params)]
    # no changes(): a site without categories exists only inside this transaction, so "exists and has none yet" is exact
    if p["tags"]:
        sel_t = " UNION ALL ".join("SELECT ?,?,?" for _ in p["tags"])
        out.append((f"INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) SELECT * FROM ({sel_t}) "
                    "WHERE EXISTS (SELECT 1 FROM sites WHERE id=?) AND NOT EXISTS (SELECT 1 FROM site_tags WHERE site_id=?)",
                    [x for t in p["tags"] for x in (p["id"], t[0], t[1])] + [p["id"], p["id"]]))
    sel_c = " UNION ALL ".join("SELECT ? AS site_id,? AS category_id" if i == 0 else "SELECT ?,?" for i in range(len(p["cats"])))
    out.append((f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT * FROM ({sel_c}) "
                "WHERE EXISTS (SELECT 1 FROM sites WHERE id=?) AND NOT EXISTS (SELECT 1 FROM site_categories WHERE site_id=?) "
                "AND category_id IN (SELECT id FROM categories)", [x for c in p["cats"] for x in (p["id"], c)] + [p["id"], p["id"]]))
    # fail-safe: a sites row without any category aborts (NOT NULL violation) and rolls the whole batch back
    out.append(("INSERT INTO site_media_jobs(identity_key,site_id,kind) SELECT NULL,id,'favicon' FROM sites "
                "WHERE id=? AND NOT EXISTS (SELECT 1 FROM site_categories WHERE site_id=?)", [p["id"], p["id"]]))
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
    match = f"(id=? OR progress_key=? OR {norm_sql('url')} IN ({kin}))"
    mparams = [p["id"], p["pkey"]] + p["keys"]
    if not new_site and p["adult"] is not None:
        # same as the old export: classify a listed site only while its flag is unknown (NULL); a set value is never overwritten.
        # The sites_media_adult_au trigger (0035) keeps the thumbnail job (eligible / excluded) in step.
        out.append((f"UPDATE sites SET adult_content=? WHERE adult_content IS NULL AND {match}", [p["adult"]] + mparams))
    out.append((f"UPDATE url_progress SET state='published',updated_at=datetime('now') WHERE identity_key=? AND state='approved' "
                f"AND EXISTS (SELECT 1 FROM sites WHERE {match})", [p["identity_key"]] + mparams))
    return out


def hold_statements(identity_key, reason):
    """Back to 'candidate' with a reason; review_state goes to 'pending' so the approve step does not re-approve it unseen."""
    return [("UPDATE url_candidates SET review_reason=?,review_state='pending',updated_at=datetime('now') WHERE identity_key=? "
             "AND EXISTS (SELECT 1 FROM url_progress WHERE identity_key=? AND state='approved')", [reason, identity_key, identity_key]),
            ("UPDATE url_progress SET state='candidate',updated_at=datetime('now') WHERE identity_key=? AND state='approved'", [identity_key])]


def existing_site(client, p):
    kin = ",".join("?" * len(p["keys"]))
    rows = client.query(f"SELECT id FROM sites WHERE id=? OR progress_key=? OR {norm_sql('url')} IN ({kin}) LIMIT 1", [p["id"], p["pkey"]] + p["keys"])
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
def guards_pass(client, p):
    """The blocked-host / rejected-request guards of the INSERT, as a read (dry-run uses it so would_publish matches the real run)."""
    params = []
    bl = blocked_sql(p["cands"], params)
    ro = requested_off_sql(p["keys"], params)
    return bool(client.query(f"SELECT 1 WHERE NOT {bl} AND NOT {ro}", params))


def run(client, *, limit, apply, log=print, heartbeat=None):
    # oldest url_progress.updated_at first: a row whose batch keeps failing is touched below, so it goes to the back of the queue
    rows = client.query(
        "SELECT p.identity_key,c.* FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key "
        "WHERE p.state='approved' AND c.review_state='approved' ORDER BY p.updated_at,p.identity_key LIMIT ?", [int(limit)])
    res = {"selected": len(rows), "published": 0, "already_listed": 0, "held": 0, "errors": 0, "dry_run": not apply,
           "would_publish": 0, "repaired": {"name": 0, "description": 0}, "held_reasons": {}, "verify_problems": {}, "integrity": {}, "ids": [], "stopped": False}
    touched = []

    def hold(identity_key, reason):
        res["held"] += 1
        res["held_reasons"][reason] = res["held_reasons"].get(reason, 0) + 1
        log(f"hold {identity_key}: {reason}")
        if apply:
            client.batch(hold_statements(identity_key, reason))

    def failed(identity_key, e):
        res["errors"] += 1
        log(f"error {identity_key}: {e}")
        try:
            client.query("UPDATE url_progress SET updated_at=datetime('now') WHERE identity_key=? AND state='approved'", [identity_key])
        except Exception:
            pass

    try:
        for row in rows:
            if heartbeat:
                heartbeat()   # raises when the runner lost its lease: stop before the next site's batch
            try:
                p = plan(dict(row))
                have_cats = {r["id"] for r in client.query(f"SELECT id FROM categories WHERE id IN ({','.join('?' * len(p['cats']))})", p["cats"])}
                p["cats"] = [c for c in p["cats"] if c in have_cats]
                if not p["cats"]:
                    raise Gate("gate:no_category")   # in categories.csv but not in this D1: never publish a site without a category
                stmts = publish_statements(p)
                if max(len(s[1]) for s in stmts) > MAX_PARAMS:
                    raise Gate("gate:too_many_params")
                if existing_site(client, p):
                    res["already_listed"] += 1
                    if apply:
                        client.batch(media_and_state(p, new_site=False))   # the existing sites row is never altered (adult_content only NULL -> 0/1)
                    continue
                if not apply:
                    if guards_pass(client, p):
                        res["would_publish"] += 1
                        for k, v in p["repaired"].items():
                            res["repaired"][k] += int(v)
                    else:
                        res["held"] += 1
                        res["held_reasons"]["gate:not_inserted_blocked_or_rejected_request"] = res["held_reasons"].get("gate:not_inserted_blocked_or_rejected_request", 0) + 1
                    continue
                client.batch(stmts)
            except Gate as g:
                hold(row["identity_key"], str(g))
                continue
            except Exception as e:   # the batch is atomic: nothing of this site was written; the row stays 'approved' and is retried later
                if "NOT NULL" in str(e) and "site_media_jobs" in str(e):   # the no-category fail-safe fired (a category vanished between check and batch)
                    try:
                        hold(row["identity_key"], "gate:no_category")
                        continue
                    except Exception as e2:
                        e = e2
                failed(row["identity_key"], e)
                continue
            problems = verify(client, p)
            if problems:
                st = client.query("SELECT state FROM url_progress WHERE identity_key=?", [p["identity_key"]])
                state = st[0]["state"] if st else None
                if not client.query("SELECT 1 FROM sites WHERE id=?", [p["id"]]):
                    if state == "approved":
                        hold(p["identity_key"], "gate:not_inserted_blocked_or_rejected_request")   # skipped by a SQL guard (blocked host / request)
                        continue
                    if state == "published":   # another writer listed the same URL between the check and the batch: the guard skipped our insert
                        res["already_listed"] += 1
                        continue
                res["verify_problems"][p["id"]] = problems
                touched.append(p["id"])
                log(f"verify {p['id']}: {problems}; stopping (this stage writes public data)")
                res["stopped"] = True
                break
            res["published"] += 1
            for k, v in p["repaired"].items():
                res["repaired"][k] += int(v)
            res["ids"].append(p["id"])
            touched.append(p["id"])
    except BaseException as e:
        e.result = res   # the caller sees what was done (integrity is filled in by the finally below: same dict)
        raise
    finally:
        # an exception from a hold batch / verify must not skip the integrity check of what was already published
        if apply and touched:
            try:
                res["integrity"] = post_check(client, touched)
            except Exception as e:
                res["integrity"] = {"post_check_failed": [(type(e).__name__, str(e)[:200])]}
            for n, r in res["integrity"].items():
                log(f"integrity {n}: {r[:3]}")
        log("publish_stage: " + json.dumps({k: v for k, v in res.items() if k != "ids"}, ensure_ascii=False))
    return res
