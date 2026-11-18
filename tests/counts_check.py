"""集計表（migration 0022）= 旧 loadCategories の再帰 CTE、を検査するヘルパ（#126）。夜間の一括投入 SQL を流したあとの件数検証に使う。"""

TREE = ("WITH RECURSIVE tree(root_id, id) AS (SELECT id, id FROM categories UNION "
        "SELECT tree.root_id, c.id FROM tree JOIN categories c ON c.parent_id = tree.id) ")


def truth(db, cc):
    """旧実装の逐語（範囲 cc。None は全世界）: カテゴリ id -> 自身と全子孫の所属サイト数（重複なし）。"""
    return dict(db.execute(TREE + "SELECT c.id, COUNT(DISTINCT s.id) FROM categories c LEFT JOIN tree ON tree.root_id = c.id "
                           "LEFT JOIN site_categories sc ON sc.category_id = tree.id LEFT JOIN sites s ON s.id = sc.site_id "
                           "AND (?1 IS NULL OR s.country_code = ?1) GROUP BY c.id", (cc,)))


def cached(db, cc):
    return dict(db.execute("SELECT c.id, COALESCE(k.n, 0) FROM categories c LEFT JOIN category_counts k "
                           "ON k.category_id = c.id AND k.scope = ?", (cc or "all",)))


def check(tc, db):
    ccs = [None] + [r[0] for r in db.execute("SELECT DISTINCT country_code FROM sites WHERE country_code IS NOT NULL")]
    for cc in ccs:
        tc.assertEqual(cached(db, cc), truth(db, cc), f"category_counts != recursive CTE (scope {cc})")
    tc.assertEqual(dict(db.execute("SELECT scope, n FROM scope_counts WHERE n > 0")),
                   {"all": db.execute("SELECT COUNT(*) FROM sites").fetchone()[0],
                    **dict(db.execute("SELECT country_code, COUNT(*) FROM sites WHERE country_code IS NOT NULL GROUP BY country_code"))})
