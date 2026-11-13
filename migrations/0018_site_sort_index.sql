-- 一覧・検索のカーソル方式ページング（#51）用。並びの式（src/sites.js の SORT_KEY）と同じ式のインデックスで、
-- 2000 件規模でも全件ソートせずに先頭から LIMIT 件だけ読む。式を変えるときは両方そろえる。
CREATE INDEX IF NOT EXISTS idx_sites_sort ON sites (datetime(COALESCE(source_updated_at, registered_at)) DESC, id);
-- カテゴリ → サイトの逆引き（カテゴリ別一覧・件数）。主キーは (site_id, category_id) で category_id 先頭の検索に効かない。
CREATE INDEX IF NOT EXISTS idx_site_categories_category ON site_categories (category_id, site_id);
