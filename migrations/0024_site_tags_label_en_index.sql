-- タグ絞り込み用の逆引き索引 その2: タグの英語名 → サイト（#126 第2段。0023 の対。`label_ja = ? OR label_en = ?` の両方に索引を効かせる）。
-- 書き込み量の見積り: 索引の行数 = label_en が NULL でない site_tags の行数（10 万件で約 24 万行。現在の本番は数千行）。0023 と別の日に適用してよい。
-- 派生物（正本は site_tags）。消すには DROP INDEX idx_site_tags_label_en。
CREATE INDEX IF NOT EXISTS idx_site_tags_label_en ON site_tags (label_en, site_id);
