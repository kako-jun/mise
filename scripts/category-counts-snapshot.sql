-- Snapshot of the count tables (Issue #137). Run before and after scripts/fix-redundant-categories.sql and compare the two outputs:
-- they must be IDENTICAL: the cleanup deletes only an ancestor row next to its descendant, and an ancestor's count already counts its
-- descendants, so no row of category_counts may change. Any difference = stop and investigate.
--   npx wrangler d1 execute mise --local --json --file scripts/category-counts-snapshot.sql > /tmp/before.json   (and after.json)
--   diff /tmp/before.json /tmp/after.json   # no output = unchanged
SELECT category_id, scope, n FROM category_counts ORDER BY category_id, scope;
