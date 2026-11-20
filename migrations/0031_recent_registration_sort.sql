-- 新着一覧/縦ビュー用。登録日の範囲と順番を同じ索引で引く。
CREATE INDEX IF NOT EXISTS idx_sites_recent_sort ON sites (datetime(registered_at) DESC, id) WHERE bulk_import = 0;
CREATE INDEX IF NOT EXISTS idx_sites_country_recent_sort ON sites (country_code, datetime(registered_at) DESC, id) WHERE bulk_import = 0;
CREATE INDEX IF NOT EXISTS idx_sites_region_recent_sort ON sites (region_code, datetime(registered_at) DESC, id) WHERE bulk_import = 0;
