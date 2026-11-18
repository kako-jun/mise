-- One-off cleanup of redundant category memberships (Issue #137). Brings the existing site_categories to the canonical form
-- that judge.normalize_categories gives new judgements. Public bodies (pb-) are never touched.
--
--   (a) a site holds a category AND one of its descendants -> delete the ancestor row (the child alone is enough)
--   (b) a site holds a retreat category (business / society / business-services / community; the fallback rows of
--       data/category-map.csv) AND any other category -> delete the retreat row
--
-- Each statement is a single plain DELETE (no compound SELECT: D1 production limits the number of terms). Idempotent: a second run
-- deletes nothing. A site never loses its last category (a keeps the descendant, b keeps the other category).
-- The count tables follow by their triggers; an ancestor's count counts descendants, so category_counts must be identical before and
-- after: take scripts/category-counts-snapshot.sql before and after and compare (docs/operations/deploy.md).
--
-- Apply to the local D1 first. Production (--remote) only after approval, with the Time Travel bookmark noted beforehand.

DELETE FROM site_categories WHERE site_id NOT LIKE 'pb-%' AND EXISTS (SELECT 1 FROM site_categories b JOIN category_closure cl ON cl.ancestor_id=site_categories.category_id AND cl.descendant_id=b.category_id AND cl.descendant_id<>cl.ancestor_id WHERE b.site_id=site_categories.site_id);

DELETE FROM site_categories WHERE site_id NOT LIKE 'pb-%' AND category_id IN ('business','society','business-services','community') AND EXISTS (SELECT 1 FROM site_categories b WHERE b.site_id=site_categories.site_id AND b.category_id NOT IN ('business','society','business-services','community'));
