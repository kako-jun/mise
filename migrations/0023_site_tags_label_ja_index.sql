-- タグ絞り込み用の逆引き索引 その1: タグの日本語名 → サイト（#126 第2段。src/sites.js の planQuery が稀なタグをここから引く）。
-- 目的: `?tag=` が稀なタグのとき、全サイトを走査して site_tags を引く形（10 万件で 85 万行読む）をやめ、タグの索引から該当サイトだけを引く（約 300 行）。
-- 書き込み量の見積り: 索引の行数 = site_tags の行数（1 サイトあたり平均約 2.4 行。10 万件で約 24 万行、現在の本番は数千行）。
--   Free の書き込み枠は 1 日 10 万行なので、件数が増えてから入れるなら日をまたいで適用する（docs/operations/deploy.md「索引の小分け適用」）。
--   英語名の索引（0024）と活動の索引（0025）は別 migration に分けてあり、1 本ずつ別の日に適用してよい。順序の依存はない（コードは索引が無くても結果は同じで、遅くなるだけ）。
-- 派生物（正本は site_tags）。消すには DROP INDEX idx_site_tags_label_ja。
CREATE INDEX IF NOT EXISTS idx_site_tags_label_ja ON site_tags (label_ja, site_id);
