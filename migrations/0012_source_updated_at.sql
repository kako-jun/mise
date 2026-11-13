-- サイト自身の更新日と根拠。確認できたものだけ入れ、取れないものは NULL のまま（推測しない）。
-- 取得は scripts/collect-source-updated.py（結果は data/source-updated.json）。
ALTER TABLE sites ADD COLUMN source_updated_at TEXT;
ALTER TABLE sites ADD COLUMN source_updated_evidence_url TEXT;
UPDATE sites SET source_updated_at='2026-06-20', source_updated_evidence_url='https://llll-ll.com/sitemap.xml' WHERE id='sample-1'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2026-06-20', source_updated_evidence_url='https://hn.llll-ll.com/rss' WHERE id='sample-2'; -- feed-latest
UPDATE sites SET source_updated_at='2025-12-10', source_updated_evidence_url='https://3min.llll-ll.com/sitemap.xml' WHERE id='sample-3'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2025-12-10', source_updated_evidence_url='https://agasteer.llll-ll.com/sitemap.xml' WHERE id='sample-4'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2026-11-10', source_updated_evidence_url='https://www.nao.ac.jp/sitemap.xml' WHERE id='sample-7'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2019-09-01', source_updated_evidence_url='https://www.genbu.net/sitemap.xml' WHERE id='collected-genbu'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2026-11-13', source_updated_evidence_url='https://kofun.info/sitemap.xml' WHERE id='collected-kofun-map'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2021-12-08', source_updated_evidence_url='https://sora-can.com/sitemap.xml' WHERE id='collected-sora-can'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2026-09-12', source_updated_evidence_url='https://www.mlum-factory.com/sitemap.xml' WHERE id='collected-mlum-factory'; -- sitemap-lastmod
UPDATE sites SET source_updated_at='2026-10-25', source_updated_evidence_url='https://tanken.com/' WHERE id='collected-tanken-com'; -- page-text-last-updated
UPDATE sites SET source_updated_at='2026-11-10', source_updated_evidence_url='https://arch-hiroshima.net/sitemap_index.xml' WHERE id='collected-arch-hiroshima'; -- sitemap-lastmod
