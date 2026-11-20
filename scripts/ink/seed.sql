-- 上下センタリングの画素測定 (#183) 用の seed。ローカル D1 専用（--local）。ids は ink で始まる。
DELETE FROM site_tags WHERE site_id LIKE 'ink%'; DELETE FROM site_categories WHERE site_id LIKE 'ink%'; DELETE FROM sites WHERE id LIKE 'ink%';
INSERT INTO sites (id,url,name,description_ja,description_en,country_code,region_code,registered_at,search_text) VALUES
 ('ink1','https://example.com/ink1','寺町の寺','寺院の紹介文です。','Temple','JP','JP-13','2026-11-01T00:00:00Z','寺 temple ink1'),
 ('ink2','https://example.com/ink2','Rust Home','Rust の紹介。','Rust home','US','US-CA','2026-11-02T00:00:00Z','寺 rust ink2'),
 ('ink3','https://example.com/ink3','大阪の寺院','紹介','Osaka','JP','JP-27','2026-11-03T00:00:00Z','寺 osaka ink3'),
 ('ink4','https://example.com/ink4','Gypsy Quip','Descender test','Descender test','JP','JP-13','2026-11-04T00:00:00Z','寺 gypsy ink4'),
 ('ink5','https://example.com/ink5','Roma Test','イタリアの例','Italy example','IT',NULL,'2026-11-05T00:00:00Z','寺 roma ink5');
INSERT INTO site_categories VALUES ('ink1','cities'),('ink2','cities'),('ink3','cities'),('ink4','cities'),('ink5','cities');
INSERT INTO site_tags (site_id,label_ja,label_en) VALUES
 ('ink1','Web','Web'),('ink1','寺院','Temple'),('ink1','Linux入門','Linux basics'),('ink1','ブログ😀','Blog😀'),('ink1','Rust','Rust'),
 ('ink2','Web','Web'),('ink2','Rust','Rust'),('ink3','寺院','Temple'),('ink4','Typography','Typography');
