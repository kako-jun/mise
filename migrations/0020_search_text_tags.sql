-- タグ（site_tags の label_ja / label_en）も検索（/api/sites?q=）の対象にする（#31 第1段）。search_text の末尾に「 日本語名 英語名」をタグの数だけ足す。
-- 暫定値（ASCII の小文字化）。NFKC・非 ASCII の大小は scripts/refresh-search-text.mjs が埋め直す（そちらも同じ並びでタグを含める）。
-- 前提: 0018 が search_text 列とトリガを作り、0007 が site_tags を作る。
DROP TRIGGER IF EXISTS sites_search_text_ai;
DROP TRIGGER IF EXISTS sites_search_text_au;
-- sites 側のトリガは、タグも含めて暫定値を作り直す（サイトの INSERT 時はタグがまだ無いのが普通で、タグの INSERT が下のトリガで足す）。
CREATE TRIGGER sites_search_text_ai AFTER INSERT ON sites WHEN NEW.search_text IS NULL BEGIN
  UPDATE sites SET search_text = lower(NEW.name || ' ' || NEW.description_ja || ' ' || COALESCE(NEW.description_en, '') || ' ' || NEW.url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = NEW.id), '')) WHERE id = NEW.id;
END;
-- search_text も同時に変えた UPDATE（明示した値）は上書きしない。
CREATE TRIGGER sites_search_text_au AFTER UPDATE OF name, description_ja, description_en, url ON sites WHEN NEW.search_text IS OLD.search_text BEGIN
  UPDATE sites SET search_text = lower(NEW.name || ' ' || NEW.description_ja || ' ' || COALESCE(NEW.description_en, '') || ' ' || NEW.url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = NEW.id), '')) WHERE id = NEW.id;
END;
-- タグの追加・変更・削除で、そのサイトの search_text を暫定値で作り直す（タグ側の操作は search_text を明示できないので常に作り直す。正規化版は埋め直しで戻る）。
CREATE TRIGGER site_tags_search_text_ai AFTER INSERT ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = sites.id), '')) WHERE id = NEW.site_id;
END;
CREATE TRIGGER site_tags_search_text_au AFTER UPDATE ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = sites.id), '')) WHERE id = NEW.site_id;
  UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = sites.id), '')) WHERE id = OLD.site_id;
END;
CREATE TRIGGER site_tags_search_text_ad AFTER DELETE ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = sites.id), '')) WHERE id = OLD.site_id;
END;
-- 既存行の埋め直し（暫定値）。
UPDATE sites SET search_text = lower(name || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(t.label_ja || ' ' || COALESCE(t.label_en, ''), ' ') FROM site_tags t WHERE t.site_id = sites.id), ''));
