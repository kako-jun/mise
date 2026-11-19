-- 範囲を国から都道府県・州まで広げる（#127）。新しい列は足さず、sites.region_code（ISO 3166-2 第1階層。例 JP-13）で絞る。
-- 集計表（0022: category_counts・scope_counts）の scope 値に区画コードを足す: 'all' / 国（country_code。例 JP）/ 区画（region_code。例 JP-13）。
--   区画は国の部分集合（region_code は country_code が必須で先頭 2 文字が一致する。0013 の CHECK）。国だけ分かって区画が NULL のサイトは、国の行にだけ数える。
--   意味は 0022 と同じ（category_counts.n = そのカテゴリ自身と全子孫の所属サイト数、重複なし）。
-- 順序: 旧トリガを消す → 既存サイトの区画分を初期集計 → 新トリガを作る。トリガは最後に作るので、初期集計中は発火せず二重に数えない。
-- 書き込み量の見積り（10 万件・全サイトに区画ありの最悪ケース）: 初期集計は category_counts 約 47 区画 × 数十カテゴリ + 約 47 行。
--   サイト 1 件の投入は、国の分に加えて区画の分（最大で祖先 3 × 1）が増えるだけで、0022 の見積り（+3〜+14 行）が最大 +3 行/サイト程度増える。
-- 0022 は適用後に凍結されているので、トリガは旧名のまま DROP → CREATE で作り直す（名前を変えないので deploy.md の戻し方 SQL もそのまま使える）。

DROP TRIGGER category_closure_au;
DROP TRIGGER category_counts_sc_ai;
DROP TRIGGER category_counts_sc_ad;
DROP TRIGGER category_counts_sc_au;
DROP TRIGGER category_counts_sites_ai;
DROP TRIGGER category_counts_sites_bd;
DROP TRIGGER category_counts_sites_au;

-- (1) 初期集計（区画の分だけ。scripts/rebuild-category-counts.sql の末尾と同一。tests/category-counts.test.mjs が逐語一致を検査する）。
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, s.region_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id WHERE s.region_code IS NOT NULL GROUP BY cl.ancestor_id, s.region_code;
INSERT INTO scope_counts (scope, n) SELECT region_code, COUNT(*) FROM sites WHERE region_code IS NOT NULL GROUP BY region_code;

-- (2) 区画の一覧用の索引（派生物。消すには DROP INDEX idx_sites_region_sort）。
-- 区画は全体のごく一部（北海道で 175 件など）なので、並びの索引（0019 の bulk_import 先頭の索引）を走査して region_code で絞ると、他の区画の行を大量に読み飛ばす。
-- region_code を先頭にして、同じ並び（src/sites.js の SORT_KEYS）を続ける。区画が NULL の行は載せない（部分索引）ので大きさは「区画ありのサイト数」。
-- 列は sortIndexSql()（src/sites.js）の列と同じ。SORT_KEYS を変えたら、この索引も新しい migration で作り直す（scripts/check-api-headers.mjs が一致を検査する）。
CREATE INDEX IF NOT EXISTS idx_sites_region_sort ON sites (region_code, bulk_import, datetime(COALESCE(source_updated_at, registered_at)) DESC, id) WHERE region_code IS NOT NULL;

-- (3) トリガ（0022 のものに「区画」を足したもの。範囲 = 'all'・country_code・region_code（NULL でなければ））。
-- 親の変更: 旧親・新親の祖先の件数を再計算（区画の分も）。
CREATE TRIGGER category_closure_au AFTER UPDATE OF parent_id ON categories WHEN OLD.parent_id IS NOT NEW.parent_id BEGIN
  DELETE FROM category_closure
   WHERE descendant_id IN (SELECT descendant_id FROM category_closure WHERE ancestor_id = NEW.id)
     AND ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id = NEW.id AND ancestor_id <> NEW.id);
  INSERT INTO category_closure (ancestor_id, descendant_id)
    SELECT p.ancestor_id, d.descendant_id FROM category_closure p, category_closure d WHERE p.descendant_id = NEW.parent_id AND d.ancestor_id = NEW.id;
  DELETE FROM category_counts WHERE category_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id));
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id)) GROUP BY cl.ancestor_id;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.country_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id)) GROUP BY cl.ancestor_id, s.country_code;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.region_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.region_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id)) GROUP BY cl.ancestor_id, s.region_code;
END;

-- サイトの所属カテゴリの追加。
CREATE TRIGGER category_counts_sc_ai AFTER INSERT ON site_categories BEGIN
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, sc.scope, 1
    FROM category_closure cl,
         (SELECT 'all' AS scope FROM sites WHERE id = NEW.site_id
          UNION ALL SELECT country_code FROM sites WHERE id = NEW.site_id AND country_code IS NOT NULL
          UNION ALL SELECT region_code FROM sites WHERE id = NEW.site_id AND region_code IS NOT NULL) sc
    WHERE cl.descendant_id = NEW.category_id
      AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = NEW.site_id) = 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
-- 所属カテゴリの削除。
CREATE TRIGGER category_counts_sc_ad AFTER DELETE ON site_categories BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN (SELECT 'all' FROM sites WHERE id = OLD.site_id
                   UNION ALL SELECT country_code FROM sites WHERE id = OLD.site_id AND country_code IS NOT NULL
                   UNION ALL SELECT region_code FROM sites WHERE id = OLD.site_id AND region_code IS NOT NULL)
     AND category_id IN (SELECT cl.ancestor_id FROM category_closure cl WHERE cl.descendant_id = OLD.category_id
          AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = OLD.site_id) = 0);
END;
-- 所属行そのものの書き換え: 旧・新の祖先の件数を再計算する。
CREATE TRIGGER category_counts_sc_au AFTER UPDATE ON site_categories BEGIN
  DELETE FROM category_counts WHERE category_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id));
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id)) GROUP BY cl.ancestor_id;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.country_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id)) GROUP BY cl.ancestor_id, s.country_code;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.region_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.region_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id)) GROUP BY cl.ancestor_id, s.region_code;
END;

-- サイト自身。INSERT: 範囲件数（all・国・区画）と、先に入っていた所属行の分の件数を足す。
CREATE TRIGGER category_counts_sites_ai AFTER INSERT ON sites BEGIN
  INSERT INTO scope_counts (scope, n)
    SELECT 'all', 1 UNION ALL SELECT NEW.country_code, 1 WHERE NEW.country_code IS NOT NULL UNION ALL SELECT NEW.region_code, 1 WHERE NEW.region_code IS NOT NULL
    ON CONFLICT (scope) DO UPDATE SET n = n + 1;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT a.ancestor_id, sc.scope, 1
    FROM (SELECT DISTINCT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id) a,
         (SELECT 'all' AS scope UNION ALL SELECT NEW.country_code WHERE NEW.country_code IS NOT NULL UNION ALL SELECT NEW.region_code WHERE NEW.region_code IS NOT NULL) sc
    WHERE 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
-- DELETE は BEFORE: 所属行が残っているうちに、残りの分の件数を引く。
CREATE TRIGGER category_counts_sites_bd BEFORE DELETE ON sites BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN ('all', OLD.country_code, OLD.region_code)
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = OLD.id);
  UPDATE scope_counts SET n = n - 1 WHERE scope IN ('all', OLD.country_code, OLD.region_code);
END;
-- 国・区画の変更: 旧い範囲から新しい範囲へ、そのサイトが属する祖先（重複なし）の件数と範囲件数を移す。変わらない範囲（国のまま区画だけ変わる場合の国）には触れない。
CREATE TRIGGER category_counts_sites_au AFTER UPDATE OF country_code, region_code ON sites
  WHEN OLD.country_code IS NOT NEW.country_code OR OLD.region_code IS NOT NEW.region_code BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN (OLD.country_code, OLD.region_code) AND scope IS NOT NEW.country_code AND scope IS NOT NEW.region_code
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id);
  INSERT INTO category_counts (category_id, scope, n)
    SELECT a.ancestor_id, ns.scope, 1
    FROM (SELECT DISTINCT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id) a,
         (SELECT NEW.country_code AS scope WHERE NEW.country_code IS NOT NULL AND NEW.country_code IS NOT OLD.country_code
          UNION ALL SELECT NEW.region_code WHERE NEW.region_code IS NOT NULL AND NEW.region_code IS NOT OLD.region_code) ns
    WHERE 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
  UPDATE scope_counts SET n = n - 1 WHERE scope IN (OLD.country_code, OLD.region_code) AND scope IS NOT NEW.country_code AND scope IS NOT NEW.region_code;
  INSERT INTO scope_counts (scope, n)
    SELECT ns.scope, 1
    FROM (SELECT NEW.country_code AS scope WHERE NEW.country_code IS NOT NULL AND NEW.country_code IS NOT OLD.country_code
          UNION ALL SELECT NEW.region_code WHERE NEW.region_code IS NOT NULL AND NEW.region_code IS NOT OLD.region_code) ns
    WHERE 1
    ON CONFLICT (scope) DO UPDATE SET n = n + 1;
END;
