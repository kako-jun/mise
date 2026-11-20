-- サイト名を言語ごとに持つ（#167）。name_ja = 日本語の画面で出す名前、name_en = 英語の画面で出す名前（どちらも NULL 可）。
-- `name` は従来どおり残す（言語共通のフォールバック。画面は「画面の言語の名前 ?? name」で出すので、どの言語でも名前は消えない）。既存行は NULL のまま。
-- 検索: search_text は `name` の直後に「 name_ja」「 name_en」を足す（NULL のときは何も足さない = 両列が NULL のサイトの search_text は従来と文字列まで同じ）。
--   0018・0020 のトリガ（sites の INSERT/UPDATE とタグの INSERT/UPDATE/DELETE）を、この式で作り直す。sites の UPDATE トリガは `UPDATE OF` に name_ja, name_en を足す。
--   暫定値（ASCII の小文字化）。NFKC・非 ASCII の大小は scripts/refresh-search-text.mjs が埋め直す（src/sites.js の searchText と同じ並び）。
--   0027 の sites_fts_au は search_text の変更に追従する（入れ子で UPDATE OF search_text が発火）。
-- ★ 既存行の search_text はこの migration では書き直さない（name_ja/name_en は全行 NULL で、式の結果は今と同じ）。名前を入れた行はトリガが暫定値にし、refresh-search-text.mjs が正規化版に戻す。
-- ★ 戻し方: トリガを 0020 の定義に戻す（0020 の CREATE TRIGGER / DROP TRIGGER を再実行）→ ALTER TABLE sites DROP COLUMN name_ja; DROP COLUMN name_en;（Worker が列を読むので、戻す前に旧 Worker へ戻す）。
ALTER TABLE sites ADD COLUMN name_ja TEXT;
ALTER TABLE sites ADD COLUMN name_en TEXT;
DROP TRIGGER IF EXISTS sites_search_text_ai;
CREATE TRIGGER sites_search_text_ai AFTER INSERT ON sites WHEN NEW.search_text IS NULL BEGIN
  UPDATE sites SET search_text = lower(NEW.name || COALESCE(' ' || NULLIF(NEW.name_ja, ''), '') || COALESCE(' ' || NULLIF(NEW.name_en, ''), '') || ' ' || NEW.description_ja || ' ' || COALESCE(NEW.description_en, '') || ' ' || NEW.url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = NEW.id ORDER BY t.label_ja)), '')) WHERE id = NEW.id;
END;
-- search_text も同時に変えた UPDATE（明示した値）は上書きしない。
DROP TRIGGER IF EXISTS sites_search_text_au;
CREATE TRIGGER sites_search_text_au AFTER UPDATE OF name, name_ja, name_en, description_ja, description_en, url ON sites WHEN NEW.search_text IS OLD.search_text BEGIN
  UPDATE sites SET search_text = lower(NEW.name || COALESCE(' ' || NULLIF(NEW.name_ja, ''), '') || COALESCE(' ' || NULLIF(NEW.name_en, ''), '') || ' ' || NEW.description_ja || ' ' || COALESCE(NEW.description_en, '') || ' ' || NEW.url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = NEW.id ORDER BY t.label_ja)), '')) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS site_tags_search_text_ai;
CREATE TRIGGER site_tags_search_text_ai AFTER INSERT ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || COALESCE(' ' || NULLIF(name_ja, ''), '') || COALESCE(' ' || NULLIF(name_en, ''), '') || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = sites.id ORDER BY t.label_ja)), '')) WHERE id = NEW.site_id;
END;
DROP TRIGGER IF EXISTS site_tags_search_text_au;
CREATE TRIGGER site_tags_search_text_au AFTER UPDATE ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || COALESCE(' ' || NULLIF(name_ja, ''), '') || COALESCE(' ' || NULLIF(name_en, ''), '') || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = sites.id ORDER BY t.label_ja)), '')) WHERE id = NEW.site_id;
  UPDATE sites SET search_text = lower(name || COALESCE(' ' || NULLIF(name_ja, ''), '') || COALESCE(' ' || NULLIF(name_en, ''), '') || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = sites.id ORDER BY t.label_ja)), '')) WHERE id = OLD.site_id;
END;
DROP TRIGGER IF EXISTS site_tags_search_text_ad;
CREATE TRIGGER site_tags_search_text_ad AFTER DELETE ON site_tags BEGIN
  UPDATE sites SET search_text = lower(name || COALESCE(' ' || NULLIF(name_ja, ''), '') || COALESCE(' ' || NULLIF(name_en, ''), '') || ' ' || description_ja || ' ' || COALESCE(description_en, '') || ' ' || url || COALESCE((SELECT ' ' || group_concat(l, ' ') FROM (SELECT t.label_ja || ' ' || COALESCE(t.label_en, '') AS l FROM site_tags t WHERE t.site_id = sites.id ORDER BY t.label_ja)), '')) WHERE id = OLD.site_id;
END;
-- ★ 0027 の FTS 同期トリガを、この後ろで同じ定義のまま作り直す。同じ表・同じイベントのトリガは「後から作ったものが先に」発火するので、
--   search_text のトリガを上で作り直すと、FTS のトリガより後ろ（= 先に発火）になり、INSERT で search_text が埋まる前の値を索引に入れて索引が壊れる。
--   作成順を 0018/0020 → 0027 に戻す（tests/name-per-language.test.mjs が INSERT/UPDATE 後の索引の整合を検査する）。定義は 0027 と同一。
DROP TRIGGER IF EXISTS sites_fts_ai;
CREATE TRIGGER sites_fts_ai AFTER INSERT ON sites WHEN (SELECT ready FROM fts_state WHERE name = 'sites_fts') = 1 BEGIN
  INSERT INTO sites_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
DROP TRIGGER IF EXISTS sites_fts_ad;
CREATE TRIGGER sites_fts_ad AFTER DELETE ON sites WHEN (SELECT ready FROM fts_state WHERE name = 'sites_fts') = 1 BEGIN
  INSERT INTO sites_fts (sites_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
END;
-- 値が変わらない UPDATE（refresh-search-text の同値書き込み・同じ紹介文の再保存）では索引に触らない（触ると 1 行あたり数行〜十数行の書き込みになる）。
DROP TRIGGER IF EXISTS sites_fts_au;
CREATE TRIGGER sites_fts_au AFTER UPDATE OF search_text ON sites WHEN new.search_text IS NOT old.search_text AND (SELECT ready FROM fts_state WHERE name = 'sites_fts') = 1 BEGIN
  INSERT INTO sites_fts (sites_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
  INSERT INTO sites_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
