"""counts_check（集計表 = 再帰 CTE の検査）自身が、重複排除の壊れを検出できること（#126）。
同じサイトが同じ祖先の下の複数の子孫に属する状態を作り、(1) 正しい集計では通り、(2) 重複して数えた集計（COUNT(DISTINCT) を COUNT にした場合の値）では落ちる。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for sub in ("scripts/llm", "scripts/pipeline"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import counts_check  # noqa: E402
import export_sites  # noqa: E402


class CountsCheckDetectsDoubleCounting(unittest.TestCase):
    def setUp(self):
        self.db = export_sites.load_db(ROOT)
        self.addCleanup(self.db.close)
        a, b, parent = self.db.execute(
            "SELECT x.id, y.id, x.parent_id FROM categories x JOIN categories y ON y.parent_id = x.parent_id AND y.id > x.id "
            "WHERE x.parent_id IS NOT NULL LIMIT 1").fetchone()
        self.parent = parent
        for sid, cc in (("dup1", "JP"), ("dup2", "US")):
            self.db.execute("INSERT INTO sites (id,url,name,description_ja,country_code) VALUES (?,?,?,?,?)", (sid, f"https://{sid}.example/", sid, "d", cc))
            self.db.executemany("INSERT INTO site_categories (site_id,category_id) VALUES (?,?)", [(sid, a), (sid, b)])

    def test_correct_counts_pass(self):
        counts_check.check(self, self.db)

    def test_double_counted_parent_fails(self):
        for scope in ("all", "JP"):
            with self.subTest(scope=scope):
                self.db.execute("UPDATE category_counts SET n = n + 1 WHERE category_id = ? AND scope = ?", (self.parent, scope))
                with self.assertRaises(AssertionError):
                    counts_check.check(self, self.db)
                self.db.execute("UPDATE category_counts SET n = n - 1 WHERE category_id = ? AND scope = ?", (self.parent, scope))
                counts_check.check(self, self.db)


if __name__ == "__main__":
    unittest.main()
