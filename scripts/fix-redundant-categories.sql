-- One-off cleanup of redundant category memberships (Issue #137). Brings the existing site_categories to the canonical form
-- that judge.normalize_categories gives new judgements. Public bodies (pb-) are never touched.
--
--   a site holds a category AND one of its descendants -> delete the ancestor row (the child alone is enough)
--
-- Retreat categories (business / society / business-services / community) are deliberately NOT removed: in the current tree they are
-- ordinary categories the LLM chooses (assign=llm), and the sites already re-judged hold them by the LLM's own choice
-- (e.g. jobs + business-services, a community hall + community). Dropping them would throw correct classifications away.
--
-- One plain DELETE (no compound SELECT: D1 production limits the number of terms). Idempotent: a second run deletes nothing. A site
-- never loses its last category (the descendant stays). An ancestor's count counts its descendants, so every row of category_counts
-- and scope_counts is IDENTICAL before and after: take scripts/category-counts-snapshot.sql before and after and diff the two
-- outputs; any difference means stop (docs/operations/deploy.md).
--
-- Apply to the local D1 first. Production (--remote) only after approval, with the Time Travel bookmark noted beforehand.

DELETE FROM site_categories WHERE site_id NOT LIKE 'pb-%' AND EXISTS (SELECT 1 FROM site_categories b JOIN category_closure cl ON cl.ancestor_id=site_categories.category_id AND cl.descendant_id=b.category_id AND cl.descendant_id<>cl.ancestor_id WHERE b.site_id=site_categories.site_id);
