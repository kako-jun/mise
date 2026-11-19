-- 0026（区画の範囲。#127）の戻し: コードを戻したあと、集計表を 0022 の状態（国と all だけ）へ戻す。
-- 区画対応のトリガ 7 本を消して 0022 のトリガ（下は 0022_category_counts.sql の逐語。tests/region-scope.test.mjs が一致を検査する）を作り直し、
-- 区画の行と索引を消す。0026 の migration 記録（d1_migrations）は残るが、これを流せば旧 integrity.py（区画を知らない版）でも 0 error になる。
-- 実行: npx wrangler d1 execute mise --remote --file scripts/rollback-region-counts.sql（承認を取ってから。夜間投入を止めた時間に）。手順は docs/operations/deploy.md。
DROP TRIGGER category_closure_au;
DROP TRIGGER category_counts_sc_ai;
DROP TRIGGER category_counts_sc_ad;
DROP TRIGGER category_counts_sc_au;
DROP TRIGGER category_counts_sites_ai;
DROP TRIGGER category_counts_sites_bd;
DROP TRIGGER category_counts_sites_au;
DELETE FROM category_counts WHERE scope LIKE '%-%';
DELETE FROM scope_counts WHERE scope LIKE '%-%';
DROP INDEX IF EXISTS idx_sites_region_sort;
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
END;
CREATE TRIGGER category_counts_sc_ai AFTER INSERT ON site_categories BEGIN
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, sc.scope, 1
    FROM category_closure cl,
         (SELECT 'all' AS scope FROM sites WHERE id = NEW.site_id UNION ALL SELECT country_code FROM sites WHERE id = NEW.site_id AND country_code IS NOT NULL) sc
    WHERE cl.descendant_id = NEW.category_id
      AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = NEW.site_id) = 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER category_counts_sc_ad AFTER DELETE ON site_categories BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN (SELECT 'all' FROM sites WHERE id = OLD.site_id UNION ALL SELECT country_code FROM sites WHERE id = OLD.site_id AND country_code IS NOT NULL)
     AND category_id IN (SELECT cl.ancestor_id FROM category_closure cl WHERE cl.descendant_id = OLD.category_id
          AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = OLD.site_id) = 0);
END;
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
END;
CREATE TRIGGER category_counts_sites_ai AFTER INSERT ON sites BEGIN
  INSERT INTO scope_counts (scope, n) SELECT 'all', 1 UNION ALL SELECT NEW.country_code, 1 WHERE NEW.country_code IS NOT NULL
    ON CONFLICT (scope) DO UPDATE SET n = n + 1;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT a.ancestor_id, sc.scope, 1
    FROM (SELECT DISTINCT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id) a,
         (SELECT 'all' AS scope UNION ALL SELECT NEW.country_code WHERE NEW.country_code IS NOT NULL) sc
    WHERE 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER category_counts_sites_bd BEFORE DELETE ON sites BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN ('all', OLD.country_code)
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = OLD.id);
  UPDATE scope_counts SET n = n - 1 WHERE scope IN ('all', OLD.country_code);
END;
CREATE TRIGGER category_counts_sites_au AFTER UPDATE OF country_code ON sites WHEN OLD.country_code IS NOT NEW.country_code BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope = OLD.country_code
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id);
  INSERT INTO category_counts (category_id, scope, n)
    SELECT DISTINCT cl.ancestor_id, NEW.country_code, 1 FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id
    WHERE x.site_id = NEW.id AND NEW.country_code IS NOT NULL
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
  UPDATE scope_counts SET n = n - 1 WHERE scope = OLD.country_code;
  INSERT INTO scope_counts (scope, n) SELECT NEW.country_code, 1 WHERE NEW.country_code IS NOT NULL ON CONFLICT (scope) DO UPDATE SET n = n + 1;
END;
