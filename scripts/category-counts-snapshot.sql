-- Snapshot of the count tables (Issue #137). Run before and after scripts/fix-redundant-categories.sql and compare the two outputs:
-- they must be identical (the cleanup removes only rows that a descendant or a real category makes redundant).
--   npx wrangler d1 execute mise --local --json --file scripts/category-counts-snapshot.sql > /tmp/before.json   (and after.json)
--   diff /tmp/before.json /tmp/after.json   # no output = unchanged
SELECT category_id, scope, n FROM category_counts ORDER BY category_id, scope;
