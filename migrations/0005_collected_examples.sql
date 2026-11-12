-- Trial collection, source and retrieval evidence: data/collection-sample.json.
-- Explicit columns and idempotence allow upgrades from the earlier local migration.
INSERT OR IGNORE INTO sites (id,url,name,description_ja,registered_at) VALUES ('collected-hyuki','https://www.hyuki.com/','結城浩 / Hiroshi Yuki','数学やプログラミング、文章に関する読み物を公開する個人サイト。',strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES ('collected-hyuki','arts-humanities');
INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES ('collected-hyuki','computers-internet');
INSERT OR IGNORE INTO sites (id,url,name,description_ja,registered_at) VALUES ('collected-kmonos','https://www.kmonos.net/','Kazuhiro Inaba','プログラミングの記事や自作ソフト、著書を紹介する個人サイト。',strftime('%Y-%m-%dT%H:%M:%SZ','now'));
INSERT OR IGNORE INTO site_categories (site_id,category_id) VALUES ('collected-kmonos','computers-software');
