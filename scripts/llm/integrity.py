"""Data integrity checks for the sites tables (Issue #137). The single definition of every check: each one is ONE simple
SELECT (D1 production limits the number of terms of a compound SELECT, so never chain many UNION ALLs) and the correct
result is 0 rows.

Used by
  - scripts/check-data-integrity.py   read-only run against a local / remote D1 (wrangler d1 execute --json) or a sqlite file
  - scripts/llm/export_approved_sql.py, import_results.py   gate: the generated SQL is run on an in-memory copy and a
    violation it introduces stops the output (see gate())
  - docs/operations/deploy.md          the before / after check of every application refers to the script, not to copies of the SQL

severity "error" fails the check (and the gate); "warn" is printed but never fails (a site without a country is legal; a retreat
category next to a real one may be the LLM's own choice).
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts", "llm"))
import judge  # noqa: E402

_PARENT, RETREAT = judge.load_tree(ROOT)


def q(v):
    return "'" + str(v).replace("'", "''") + "'"


RETREAT_LIST = ",".join(q(r) for r in sorted(RETREAT))


def norm_sql(col):
    """SQL twin of the URL comparison key (lower case, no scheme, no leading www., %7e as ~, no trailing slash)."""
    s = f"CASE WHEN lower(trim({col})) LIKE 'https://%' THEN substr(lower(trim({col})),9) " \
        f"WHEN lower(trim({col})) LIKE 'http://%' THEN substr(lower(trim({col})),8) ELSE lower(trim({col})) END"
    return f"rtrim(replace(CASE WHEN ({s}) LIKE 'www.%' THEN substr(({s}),5) ELSE ({s}) END,'%7e','~'),'/')"


TREE = ("WITH RECURSIVE tree(root_id,id) AS (SELECT id,id FROM categories UNION "
        "SELECT tree.root_id,c.id FROM tree JOIN categories c ON c.parent_id=tree.id) ")

# ISO 3166-1 alpha-2, all 249 codes. The single list in the browser is MISE_ISO in public/countries.js (tests/country-flags.test.mjs keeps the two equal).
ISO_3166_1 = (
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT", "AU", "AW", "AX", "AZ", "BA",
    "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV",
    "BW", "BY", "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO", "CR", "CU",
    "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC", "EE", "EG", "EH", "ER", "ES",
    "ET", "FI", "FJ", "FK", "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL", "GM",
    "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM", "HN", "HR", "HT", "HU", "ID", "IE",
    "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM",
    "KN", "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY",
    "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT",
    "MU", "MV", "MW", "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU",
    "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT", "PW", "PY", "QA",
    "RE", "RO", "RS", "RU", "RW", "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
    "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL",
    "TM", "TN", "TO", "TR", "TT", "TV", "TW", "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE",
    "VG", "VI", "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW",
)
ISO_LIST = ",".join("'%s'" % c for c in ISO_3166_1)

# (name, severity, what it means, SQL). Every SQL is a single SELECT whose rows are the violations.
CHECKS = [
    ("ancestor_duplicate", "error",
     "a site holds a category and one of its descendants (the child alone is enough; pb- is exempt)",
     "SELECT a.site_id, a.category_id parent_id, b.category_id child_id FROM site_categories a "
     "JOIN category_closure cl ON cl.ancestor_id=a.category_id AND cl.descendant_id<>a.category_id "
     "JOIN site_categories b ON b.site_id=a.site_id AND b.category_id=cl.descendant_id WHERE a.site_id NOT LIKE 'pb-%'"),
    ("retreat_with_real", "warn",
     f"information: a site holds a retreat category ({'/'.join(sorted(RETREAT))}) next to another category (the LLM may have chosen it; pb- is exempt)",
     f"SELECT a.site_id, a.category_id FROM site_categories a WHERE a.site_id NOT LIKE 'pb-%' AND a.category_id IN ({RETREAT_LIST}) "
     f"AND EXISTS (SELECT 1 FROM site_categories b WHERE b.site_id=a.site_id AND b.category_id NOT IN ({RETREAT_LIST}))"),
    ("uncategorized", "error", "a site without any category",
     "SELECT s.id FROM sites s WHERE NOT EXISTS (SELECT 1 FROM site_categories x WHERE x.site_id=s.id)"),
    ("orphan_site_categories", "error", "site_categories pointing at a category or a site that does not exist",
     "SELECT x.site_id, x.category_id FROM site_categories x WHERE NOT EXISTS (SELECT 1 FROM categories c WHERE c.id=x.category_id) "
     "OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.id=x.site_id)"),
    ("category_counts_all", "error", "category_counts (scope all) differs from the recursive count",
     TREE + "SELECT k.id, COALESCE(w.n,0) expected, COALESCE(cc.n,0) cached FROM categories k "
     "LEFT JOIN (SELECT t.root_id cid, COUNT(DISTINCT s.id) n FROM tree t JOIN site_categories x ON x.category_id=t.id "
     "JOIN sites s ON s.id=x.site_id GROUP BY t.root_id) w ON w.cid=k.id "
     "LEFT JOIN category_counts cc ON cc.category_id=k.id AND cc.scope='all' WHERE COALESCE(w.n,0)<>COALESCE(cc.n,0)"),
    ("category_counts_country", "error",
     "category_counts (per country) differs from the recursive count, or a non-empty row is left for a country without sites (n=0 rows are the normal empty state)",
     TREE + "SELECT COALESCE(w.cid,cc.category_id) id, COALESCE(w.scope,cc.scope) scope, COALESCE(w.n,0) expected, COALESCE(cc.n,0) cached "
     "FROM (SELECT t.root_id cid, s.country_code scope, COUNT(DISTINCT s.id) n FROM tree t JOIN site_categories x ON x.category_id=t.id "
     "JOIN sites s ON s.id=x.site_id WHERE s.country_code IS NOT NULL GROUP BY t.root_id, s.country_code) w "
     "LEFT JOIN category_counts cc ON cc.category_id=w.cid AND cc.scope=w.scope WHERE COALESCE(w.n,0)<>COALESCE(cc.n,0) "
     "UNION ALL SELECT cc.category_id, cc.scope, 0, cc.n FROM category_counts cc WHERE cc.scope<>'all' AND cc.scope NOT LIKE '%-%' AND cc.n<>0 "
     "AND NOT EXISTS (SELECT 1 FROM sites s JOIN site_categories x ON x.site_id=s.id WHERE s.country_code=cc.scope)"),
    ("category_counts_region", "error",
     "category_counts (per region, e.g. JP-13; migration 0026) differs from the recursive count, or a non-empty row is left for a region without sites (n=0 rows are the normal empty state)",
     TREE + "SELECT COALESCE(w.cid,cc.category_id) id, COALESCE(w.scope,cc.scope) scope, COALESCE(w.n,0) expected, COALESCE(cc.n,0) cached "
     "FROM (SELECT t.root_id cid, s.region_code scope, COUNT(DISTINCT s.id) n FROM tree t JOIN site_categories x ON x.category_id=t.id "
     "JOIN sites s ON s.id=x.site_id WHERE s.region_code IS NOT NULL GROUP BY t.root_id, s.region_code) w "
     "LEFT JOIN category_counts cc ON cc.category_id=w.cid AND cc.scope=w.scope WHERE COALESCE(w.n,0)<>COALESCE(cc.n,0) "
     "UNION ALL SELECT cc.category_id, cc.scope, 0, cc.n FROM category_counts cc WHERE cc.scope LIKE '%-%' AND cc.n<>0 "
     "AND NOT EXISTS (SELECT 1 FROM sites s JOIN site_categories x ON x.site_id=s.id WHERE s.region_code=cc.scope)"),
    ("scope_counts_country", "error", "scope_counts (per country and per region, e.g. JP and JP-13) differs from the number of sites",
     "SELECT sc.scope, sc.n cached, (SELECT COUNT(*) FROM sites s WHERE s.country_code=sc.scope OR s.region_code=sc.scope) expected FROM scope_counts sc "
     "WHERE sc.scope<>'all' AND sc.n<>(SELECT COUNT(*) FROM sites s WHERE s.country_code=sc.scope OR s.region_code=sc.scope)"),
    ("scope_counts_all", "error", "scope_counts (all) differs from the number of sites",
     "SELECT sc.n cached,(SELECT COUNT(*) FROM sites) expected FROM scope_counts sc "
     "WHERE sc.scope='all' AND sc.n<>(SELECT COUNT(*) FROM sites)"),
    ("category_closure", "error", "category_closure misses a pair (missing) or holds a surplus one (extra), against the recursive tree",
     TREE + "SELECT 'missing' kind, t.root_id, t.id FROM tree t WHERE NOT EXISTS (SELECT 1 FROM category_closure cl "
     "WHERE cl.ancestor_id=t.root_id AND cl.descendant_id=t.id) UNION ALL SELECT 'extra', cl.ancestor_id, cl.descendant_id "
     "FROM category_closure cl WHERE NOT EXISTS (SELECT 1 FROM tree t WHERE t.root_id=cl.ancestor_id AND t.id=cl.descendant_id)"),
    ("duplicate_site_category", "error", "the same (site, category) pair twice (the primary key should prevent it)",
     "SELECT site_id, category_id, COUNT(*) n FROM site_categories GROUP BY site_id, category_id HAVING COUNT(*)>1"),
    ("duplicate_url", "error", "two sites with the same normalized URL (host + path; scheme, www., trailing slash and case ignored)",
     f"SELECT k, COUNT(*) n FROM (SELECT {norm_sql('url')} AS k FROM sites) GROUP BY k HAVING COUNT(*)>1"),
    ("public_body_not_leaf", "error", "a public body (pb-) holding a category that is not a leaf under government",
     "SELECT x.site_id, x.category_id FROM site_categories x JOIN categories c ON c.id=x.category_id "
     "WHERE x.site_id LIKE 'pb-%' AND c.parent_id IS NOT 'government'"),
    ("empty_text", "error", "a site with an empty name or an empty Japanese description, or a name_ja / name_en that is an empty string (NULL is fine: it falls back to name)",
     "SELECT id FROM sites WHERE trim(name)='' OR trim(description_ja)='' OR trim(name_ja)='' OR trim(name_en)=''"),
    ("no_country", "warn", "a site without country_code (legal, but it appears in no country view)",
     "SELECT id FROM sites WHERE country_code IS NULL"),
    ("country_code_not_iso", "warn",
     "sites.country_code is not an ISO 3166-1 alpha-2 code (the flag and country name tables cover exactly those; anything else shows as a plain code badge)",
     f"SELECT id, country_code FROM sites WHERE country_code IS NOT NULL AND country_code NOT IN ({ISO_LIST})"),
]


def run_checks(execute, names=None):
    """execute(sql) -> list of row tuples. Returns {name: [rows]} for every check (or only the named ones)."""
    return {n: [tuple(r) for r in execute(sql)] for n, _sev, _why, sql in CHECKS if names is None or n in names}


def errors_of(result):
    sev = {n: s for n, s, _w, _q in CHECKS}
    return {n: rows for n, rows in result.items() if sev[n] == "error"}


def sqlite_execute(con):
    return lambda sql: con.execute(sql).fetchall()


_BASE = None


def base_memory_db(current=None):
    """A fresh in-memory database to run SQL on: a copy of the sqlite file `current` (e.g. a local D1 file), or all migrations applied."""
    global _BASE
    dst = sqlite3.connect(":memory:")
    if current:
        src = sqlite3.connect("file:" + os.path.abspath(current) + "?mode=ro", uri=True)
        try:
            src.backup(dst)
        finally:
            src.close()
        return dst
    if _BASE is None:
        import export_sites
        _BASE = export_sites.load_db(ROOT)
    _BASE.backup(dst)
    return dst


def gate(sql, current=None):
    """Run `sql` on an in-memory database and return the violations (severity error) that the SQL INTRODUCES, i.e. rows that
    were not there before it ran: {check name: [rows]} (empty = fine). A failure of the SQL itself is reported as
    {"sql_error": [(message,)]}. Violations that the base already had (old data waiting for the one-off cleanup) do not block."""
    con = base_memory_db(current)
    try:
        before = errors_of(run_checks(sqlite_execute(con)))
        try:
            con.executescript(sql)
        except sqlite3.Error as e:
            return {"sql_error": [(str(e),)]}
        after = errors_of(run_checks(sqlite_execute(con)))
    finally:
        con.close()
    new = {n: [r for r in rows if r not in set(before[n])] for n, rows in after.items()}
    return {n: rows for n, rows in new.items() if rows}


def format_violations(found, limit=5):
    lines = []
    for n, rows in found.items():
        lines.append(f"  {n}: {len(rows)} row(s)")
        lines += [f"    {r}" for r in rows[:limit]]
    return "\n".join(lines)
