"""カテゴリの木(data/categories.csv)・旧→新の対応表・migration 0021 の整合(Issue #114 第1段)。ネットワーク・wrangler は使わない。"""
import csv, glob, os, pathlib, re, sqlite3, sys, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
import taxonomy_sql as tx  # noqa: E402
import public_bodies_sql as psql  # noqa: E402
import judge  # noqa: E402

TREE = tx.read_tree()
MIG = ROOT / "migrations" / "0021_taxonomy.sql"


class TreeTests(unittest.TestCase):
    def test_size_and_shape(self):
        ids = [r["id"] for r in TREE]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 153)
        self.assertEqual(sum(1 for r in TREE if not r["parent_id"]), 15)

    def test_parents_come_first_and_depth_at_most_3(self):
        seen, depth = set(), {}
        for r in TREE:
            if r["parent_id"]:
                self.assertIn(r["parent_id"], seen, r["id"])
            depth[r["id"]] = 1 if not r["parent_id"] else depth[r["parent_id"]] + 1
            seen.add(r["id"])
        self.assertLessEqual(max(depth.values()), 3)  # 深さ 3 超は将来許容(成長則)だが、第1段の木は 3 まで

    def test_ids_are_short_slugs_and_do_not_collide_with_scopes(self):
        for r in TREE:
            self.assertRegex(r["id"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
            self.assertNotEqual(r["id"], "all")
            self.assertGreater(len(r["id"]), 2)  # 2 文字は国コードと衝突する
            self.assertTrue(r["name_ja"] and r["name_en"])

    def test_sort_order_is_unique_among_siblings(self):
        seen = set()
        for r in TREE:
            k = (r["parent_id"], r["sort_order"])
            self.assertNotIn(k, seen, r["id"])
            seen.add(k)

    def test_government_leaves_are_exactly_the_rule_rows(self):
        rule = {r["id"] for r in TREE if r["assign"] == "rule"}
        self.assertEqual(rule, set(tx.GOV_LEAF.values()))
        self.assertTrue(all(r["parent_id"] == "government" for r in TREE if r["assign"] == "rule"))
        llm_ids = {c["id"] for c in judge.load_categories()}
        self.assertEqual(llm_ids, {r["id"] for r in TREE} - rule)
        self.assertIn("government", llm_ids)


class PromptTests(unittest.TestCase):
    cats = judge.load_categories()

    def test_prompt_indents_by_depth_and_hides_rule_leaves(self):
        text = judge.build_system(self.cats)
        self.assertIn("\nhumanities = ", text)
        self.assertIn("\n  religion = ", text)
        self.assertIn("\n    shrines = Shinto and shrines / 神社・神道", text)
        self.assertNotIn("cities =", text)
        self.assertIn("\ngovernment = Government and public bodies", text)
        self.assertIn("deepest", text)  # 葉(最も具体的な id)を優先する指示

    def test_schema_enum_equals_prompt_ids(self):
        enum = judge.build_schema(self.cats)["properties"]["categories"]["items"]["enum"]
        self.assertEqual(set(enum), {c["id"] for c in self.cats})
        self.assertEqual(len(enum), 153 - 7)

    def test_pipeline_block_is_the_same_list(self):
        import pipeline
        self.assertEqual(pipeline.category_block(self.cats), judge.category_lines(self.cats))
        self.assertIn("each 2-space indent", pipeline.triage_system(self.cats))

    def test_prompt_size_is_about_3k_tokens(self):
        # 実測 約 3,030 トークン(count_tokens, 2026-11-18)。保守的な見積りは実測より多く、100K 階層には遠い
        est = judge.estimate_tokens(judge.build_system(self.cats))
        self.assertLess(est, 20000)


class MapTests(unittest.TestCase):
    def test_covers_every_old_category_and_targets_exist(self):
        mp = tx.read_map()
        self.assertEqual({r["old_id"] for r in mp}, set(tx.old_ids()))
        new = {r["id"] for r in TREE}
        for r in mp:
            if r["mode"] in ("keep", "map", "fallback"):
                self.assertIn(r["new_id"], new, r["old_id"])
            else:
                self.assertEqual(r["new_id"], "", r["old_id"])

    def test_keep_means_same_id(self):
        for r in tx.read_map():
            if r["mode"] == "keep":
                self.assertEqual(r["old_id"], r["new_id"])


class ModernizeTests(unittest.TestCase):
    valid = {c["id"] for c in judge.load_categories()}
    legacy = judge.load_legacy_map()

    def m(self, ids):
        return judge.modernize_categories(ids, self.valid, self.legacy)

    def test_new_ids_pass_through_and_old_ones_translate(self):
        self.assertEqual(self.m(["astronomy"]), ["astronomy"])
        self.assertEqual(self.m(["science-astronomy", "arts-photography"]), ["astronomy", "photography"])
        self.assertEqual(self.m(["science", "science-biology"]), ["biology"])  # 引き継いだ id は写るが、親(science)は子(biology)と並ぶので正規化で外れる(#137)

    def test_fallback_only_when_nothing_else_remains(self):
        self.assertEqual(self.m(["business-companies"]), ["business"])
        self.assertEqual(self.m(["business-companies", "health-hospitals"]), ["hospitals"])
        self.assertEqual(self.m(["business-organizations", "business-products-and-services"]), ["society", "business-services"])

    def test_drop_unknown_duplicates_and_limit(self):
        self.assertEqual(self.m(["computers-multimedia", "nope"]), [])
        self.assertEqual(self.m(["computers-software", "software"]), ["software"])
        self.assertEqual(self.m(["government-government-agencies"]), ["government"])  # 官公庁は行政・公的機関(トップ)へ。種別の葉は台帳で決まる(0021 の退避と同じ)
        self.assertEqual(self.m(["government-government-agencies", "science-astronomy"]), ["government", "astronomy"])
        self.assertEqual(len(self.m(["arts", "photography", "architecture", "museums"])), 3)


def fresh_db(files, upto_before_0021=False):
    """migrations を順に適用したメモリ上の DB(0006 が参照する D1 の適用履歴表は空で作る)。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    for f in files:
        if upto_before_0021 and f.endswith("0021_taxonomy.sql"):
            continue
        db.executescript(pathlib.Path(f).read_text(encoding="utf-8"))
    return db


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = sorted(glob.glob(str(ROOT / "migrations" / "*.sql")))
        cls.pre = fresh_db(files, upto_before_0021=True)
        cls.post = fresh_db(files)
        cls.addClassCleanup(cls.pre.close)
        cls.addClassCleanup(cls.post.close)

    def test_tree_in_db_equals_csv(self):
        # 木を育てる migration（0022 以降）を足すときは data/categories.csv も同じ変更にする。ここが両者のずれを止める。
        rows = self.post.execute("SELECT id,COALESCE(parent_id,''),name_ja,name_en,sort_order FROM categories ORDER BY id").fetchall()
        want = sorted((r["id"], r["parent_id"], r["name_ja"], r["name_en"], int(r["sort_order"])) for r in TREE)
        self.assertEqual(rows, want)

    def test_sites_and_tags_untouched(self):
        for t in ("sites", "site_tags"):
            self.assertEqual(self.pre.execute(f"SELECT count(*) FROM {t}").fetchone(), self.post.execute(f"SELECT count(*) FROM {t}").fetchone())
        self.assertEqual(self.pre.execute("SELECT id,search_text FROM sites ORDER BY id").fetchall(), self.post.execute("SELECT id,search_text FROM sites ORDER BY id").fetchall())

    def test_no_orphans_and_no_site_lost_its_categories(self):
        post = self.post
        self.assertEqual(post.execute("SELECT count(*) FROM site_categories WHERE category_id NOT IN (SELECT id FROM categories) OR site_id NOT IN (SELECT id FROM sites)").fetchone()[0], 0)
        had = {r[0] for r in self.pre.execute("SELECT DISTINCT site_id FROM site_categories")}
        has = {r[0] for r in post.execute("SELECT DISTINCT site_id FROM site_categories")}
        self.assertEqual(had - has, set())
        self.assertEqual(post.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_government_sites_get_the_ledger_type_leaf(self):
        ledger = psql.listed(psql.load(tx.LEDGER), False)
        leaf = {}
        for sid, cat in self.post.execute("SELECT s.id, sc.category_id FROM sites s JOIN site_categories sc ON sc.site_id=s.id WHERE sc.category_id IN (%s)" % ",".join("?" * 7), list(tx.GOV_LEAF.values())):
            leaf.setdefault(sid, set()).add(cat)
        by_url = {psql.norm(u): sid for sid, u in self.post.execute("SELECT id,url FROM sites")}
        expected = {}
        for r in ledger.values():
            sid = by_url.get(psql.norm(r["url"]))
            if sid:
                expected[sid] = {tx.GOV_LEAF[r["type"]]}
        self.assertEqual(leaf, expected)
        self.assertEqual(len(leaf), 1852)  # pb- の 1851 件 + 同じ URL で重複回避された国立国会図書館(sample-6)
        self.assertEqual(leaf["sample-6"], {"national-agencies"})
        # 政府機関の旧カテゴリだったサイトが 行政・公的機関(トップ) に直接残らない(例外 0 件)
        self.assertEqual(self.post.execute("SELECT count(*) FROM site_categories WHERE category_id='government'").fetchone()[0], 0)

    def test_old_ids_are_gone_and_kept_ids_remain(self):
        ids = {r[0] for r in self.post.execute("SELECT id FROM categories")}
        new = {r["id"] for r in TREE}
        self.assertEqual(ids, new)
        self.assertTrue({"arts", "business", "reference", "education", "science", "government", "health", "society", "entertainment"} <= ids)

    def test_fallback_gets_every_destination_and_matches_modernize(self):
        # 0021 の退避先付与(SQL)と judge.modernize_categories(Python)は同じ結果になる。本番相当の組合せ(企業+製品とサービス、団体+企業+地域 など)を全部流して比べる
        files = sorted(glob.glob(str(ROOT / "migrations" / "*.sql")))
        db = fresh_db(files, upto_before_0021=True)
        self.addCleanup(db.close)
        old = sorted(tx.old_ids())
        dropped = {r["old_id"] for r in tx.read_map() if r["mode"] == "drop"}
        combos = [
            ("business-companies", "business-products-and-services"), ("business-organizations", "business-products-and-services"),
            ("business-organizations", "business-companies"), ("business-organizations", "business-companies", "local-japan"),
            ("business-companies", "local-japan"), ("local",), ("local-japan",), ("business-products-and-services",),
            ("business-companies", "health-hospitals"), ("business", "business-companies"), ("society", "business-organizations"),
            ("business-organizations", "business-products-and-services", "business-companies"),
            ("government-government-agencies", "business-companies"), ("business-companies", "science-astronomy", "local-japan"),
        ] + [(o,) for o in old if o not in dropped]  # 行き先の無い(drop)だけのサイトは CHECK で失敗するので除く(別テスト)
        legacy = {r["old_id"]: r for r in tx.read_map()}
        legacy = {k: {"new": v["new_id"] or None, "mode": v["mode"]} for k, v in legacy.items()}
        valid = {r["id"] for r in TREE}
        leaves = set(tx.GOV_LEAF.values())
        for n, ids in enumerate(combos):
            db.execute("INSERT INTO sites (id,url,name,description_ja) VALUES (?,?,?,?)", (f"c{n}", f"https://c{n}.example/", "t", "t"))
            if "government-government-agencies" in ids:  # 台帳の種別タグ(市)を付けておく
                db.execute("INSERT INTO site_tags (site_id,label_ja,label_en) VALUES (?,?,?)", (f"c{n}", "市", "City"))
            db.executemany("INSERT INTO site_categories VALUES (?,?)", [(f"c{n}", i) for i in ids])
        db.commit()
        db.executescript(MIG.read_text(encoding="utf-8"))
        for n, ids in enumerate(combos):
            got = {("government" if c in leaves else c) for (c,) in db.execute("SELECT category_id FROM site_categories WHERE site_id=?", (f"c{n}",))}
            # 0021 は #137 より前の履歴なので正規化なし(空の木)で比べる。実データの整理は scripts/fix-redundant-categories.sql
            want = set(judge.modernize_categories(list(ids), valid, legacy, tree=({}, frozenset())))
            self.assertEqual(got, want, ids)
        two = {c for (c,) in db.execute("SELECT category_id FROM site_categories WHERE site_id='c0'")}
        self.assertEqual(two, {"business", "business-services"})
        three = {c for (c,) in db.execute("SELECT category_id FROM site_categories WHERE site_id='c3'")}
        self.assertEqual(three, {"society", "business", "community"})

    def test_failure_leaves_nothing_half_done(self):
        # 旧カテゴリしか持たない(対応表で行き先の無い)サイトがあれば、CHECK で migration 全体が失敗する(中途半端に消さない)
        db = fresh_db(sorted(glob.glob(str(ROOT / "migrations" / "*.sql"))), upto_before_0021=True)
        self.addCleanup(db.close)
        db.execute("INSERT INTO sites (id,url,name,description_ja) VALUES ('t1','https://t1.example/','t','t')")
        db.execute("INSERT INTO site_categories VALUES ('t1','computers-multimedia')")
        db.commit()
        before = db.execute("SELECT count(*) FROM categories").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            db.executescript("BEGIN;\n" + MIG.read_text(encoding="utf-8") + "\nCOMMIT;")
        db.rollback()
        self.assertEqual(db.execute("SELECT count(*) FROM categories").fetchone()[0], before)
        self.assertEqual(db.execute("SELECT count(*) FROM categories WHERE id='cities'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
