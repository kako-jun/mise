-- サイトの属する国内区画（運営者の所在する県・州。ISO 3166-2 の第1階層）。確認できないものは NULL（国旗のみ表示）。
-- country_code が必須で、先頭2文字（国コード）が一致すること。根拠は data/region-evidence.json、設計は docs/site-schema.md の「運営者の所在区画」。
ALTER TABLE sites ADD COLUMN region_code TEXT CHECK (region_code IS NULL OR (country_code IS NOT NULL AND length(region_code) BETWEEN 4 AND 6 AND region_code GLOB '[A-Z][A-Z]-[A-Z0-9]*' AND substr(region_code, 1, 2) = country_code));
-- collected-kofun-map: 運営ページに「都内在住…」 -> 東京都。
-- collected-stellarscenes: 作者紹介の居住地欄が茨城県日立市 -> 茨城県。
-- sample-6 国立国会図書館: 公式アクセスページの東京本館所在地が千代田区永田町 -> 東京都。
-- sample-7 国立天文台: 公式トップページ末尾の住所が東京都三鷹市大沢 -> 東京都。
UPDATE sites SET region_code='JP-13' WHERE id IN ('collected-kofun-map','sample-6','sample-7');
UPDATE sites SET region_code='JP-08' WHERE id='collected-stellarscenes';
