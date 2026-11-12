ALTER TABLE sites ADD COLUMN last_crawled_at TEXT;
CREATE TABLE site_tags (site_id TEXT NOT NULL REFERENCES sites(id), label_ja TEXT NOT NULL, label_en TEXT, PRIMARY KEY(site_id,label_ja));
