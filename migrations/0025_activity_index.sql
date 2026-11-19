-- 活動（HUD の直近 7 日の日別登録数。/api/activity）用の索引（#126 第2段）。
-- 目的: `bulk_import = 0 AND date(registered_at) >= date('now','-6 days')` を全件走査せずに引く（10 万件で 18 万行読む → 数百行）。
-- 書き込み量の見積り: 索引の行数 = sites の行数（10 万件で 10 万行、現在の本番は約 2,000 行）。0023・0024 と別の日に適用してよい。
-- 派生物。消すには DROP INDEX idx_sites_activity。
CREATE INDEX IF NOT EXISTS idx_sites_activity ON sites (bulk_import, date(registered_at));
