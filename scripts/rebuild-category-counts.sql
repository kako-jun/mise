-- カテゴリ件数の集計表（migration 0022。#126）の作り直し。集計表がずれたときに、正本（sites / site_categories / categories）から作り直す。
-- 実行: npx wrangler d1 execute mise --remote --file scripts/rebuild-category-counts.sql（承認を取ってから。手順は docs/operations/deploy.md）
-- 下の INSERT のうち前半 4 文は migration 0022 の「(3) 初期集計」、末尾 2 文は 0026 の「(1) 初期集計」と同一（tests/category-counts.test.mjs が逐語一致と結果の一致を検査する）。
-- トリガは表 3 つ（category_closure / category_counts / scope_counts）への書き込みでは発火しない（トリガの対象は categories / site_categories / sites）。
-- 作業中に書き込みが入ると取りこぼすので、夜間投入（--update-categories 等）を流していない時間に実行する。D1 の --file は 1 つのバッチとして実行される。
DELETE FROM category_closure;
DELETE FROM category_counts;
DELETE FROM scope_counts;
INSERT INTO category_closure (ancestor_id, descendant_id)
  WITH RECURSIVE t(a, d) AS (SELECT id, id FROM categories UNION SELECT t.a, c.id FROM t JOIN categories c ON c.parent_id = t.d)
  SELECT a, d FROM t;
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id GROUP BY cl.ancestor_id;
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id WHERE s.country_code IS NOT NULL GROUP BY cl.ancestor_id, s.country_code;
INSERT INTO scope_counts (scope, n) SELECT 'all', COUNT(*) FROM sites;
INSERT INTO scope_counts (scope, n) SELECT country_code, COUNT(*) FROM sites WHERE country_code IS NOT NULL GROUP BY country_code;
-- ここから 0026（#127）の初期集計と同一: 区画（region_code。例 JP-13）の分。
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, s.region_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id WHERE s.region_code IS NOT NULL GROUP BY cl.ancestor_id, s.region_code;
INSERT INTO scope_counts (scope, n) SELECT region_code, COUNT(*) FROM sites WHERE region_code IS NOT NULL GROUP BY region_code;
