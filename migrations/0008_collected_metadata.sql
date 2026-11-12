-- Confirmed during the trial collection; evidence is in data/collection-sample.json.
UPDATE sites SET last_crawled_at='2026-11-12T08:21:16Z' WHERE id='collected-hyuki';
UPDATE sites SET last_crawled_at='2026-11-12T08:21:19Z' WHERE id='collected-kmonos';
INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES ('collected-hyuki','個人サイト','Personal site');
INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES ('collected-hyuki','数学','Mathematics');
INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES ('collected-hyuki','プログラミング','Programming');
INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES ('collected-kmonos','個人サイト','Personal site');
INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) VALUES ('collected-kmonos','プログラミング','Programming');
