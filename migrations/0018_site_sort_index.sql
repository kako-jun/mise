-- 一覧・検索のカーソル方式ページング（#51）用。
-- 並びの式インデックス: src/sites.js の SORT_KEYS と同じ列・式・向き（sortIndexSql() が出力する文字列）。並びを変えたら新しい migration で作り直す。
CREATE INDEX IF NOT EXISTS idx_sites_sort ON sites (datetime(COALESCE(source_updated_at, registered_at)) DESC, id);
-- カテゴリ → サイトの逆引き（カテゴリ別一覧・件数）。主キーは (site_id, category_id) で category_id 先頭の検索に効かない。
CREATE INDEX IF NOT EXISTS idx_site_categories_category ON site_categories (category_id, site_id);
-- 検索用の正規化済みテキスト（名前・日英の紹介文・URL を NFKC + 小文字化したもの）。SQL の lower() は ASCII だけなので、
-- ここでは暫定値（ASCII の小文字化）を入れ、全角半角・非 ASCII の大小は scripts/refresh-search-text.mjs が埋め直す。
ALTER TABLE sites ADD COLUMN search_text TEXT;
UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url);
-- 新規 INSERT の行にも暫定値を入れる（migration で足すサイトが検索できなくならないように）。
CREATE TRIGGER sites_search_text_ai AFTER INSERT ON sites WHEN NEW.search_text IS NULL BEGIN
  UPDATE sites SET search_text = lower(NEW.name || ' ' || NEW.description_ja || ' ' || COALESCE(NEW.description_en, '') || ' ' || NEW.url) WHERE id = NEW.id;
END;
