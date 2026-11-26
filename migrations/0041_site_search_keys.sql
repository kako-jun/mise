-- 検索の関連度（#301）: 検索語と「完全に一致する」鍵の表。/api/sites?q= は、鍵が検索語と等しいサイトを先に出す（完全一致の段）。
-- 鍵と rank（小さいほど上）:
--   0 = ホスト名（小文字・先頭の www. と :ポートを除く）が完全一致し、URL がトップ（パスなし・クエリなし。末尾の / だけは可）
--   1 = ホスト名が完全一致し、URL にパスがある
--   2 / 3 = ドメインの主要部（x.com の x、example.co.jp の example）が完全一致。トップ / パスあり。
--           主要部は「ラベル 2 つ（x.com）」か「ラベル 3 つで 2 つ目が co・or・ne・ac・go・lg・ed・gr・ad・com・net・org・gov・edu（example.co.jp）」のホストだけ。
--           サブドメイン（blog.example.com・city.kanazawa.lg.jp）は主要部の鍵を持たない（名前の鍵で当たる）。
--   4 = サイト名（name・name_ja・name_en のどれか。前後の空白を除き小文字）が完全一致
-- 同じ鍵が複数の由来から出たら小さい rank を残す。小文字化は SQL の lower()（ASCII のみ）で、search_text のトリガの暫定値と同じ。
-- 派生物（正本は sites）。sites の INSERT・url/name の変更・DELETE のトリガで保つ。消すには DROP TABLE site_search_keys（トリガも DROP）で、検索は今の並びに戻る。
-- 書き込み: 既存行の埋め直しで 1 サイトあたり鍵 1〜5 行（+ site_id の索引）。
-- 鍵の由来 5 つ（ホスト名・主要部・3 つの名前）は json_each の 5 行との CROSS JOIN で 1 行ずつに展開する（複合 SELECT = UNION ALL を使わない。D1 本番は複合 SELECT の項数上限が低い前例があり、decisions.md の決定で 3 項以下にしている）。
CREATE TABLE IF NOT EXISTS site_search_keys (key TEXT NOT NULL, site_id TEXT NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY (key, site_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_site_search_keys_site ON site_search_keys (site_id);
DROP TRIGGER IF EXISTS sites_search_keys_ai;
CREATE TRIGGER sites_search_keys_ai AFTER INSERT ON sites BEGIN
  INSERT OR IGNORE INTO site_search_keys (key, site_id, rank)
    SELECT key, sid, MIN(rank) FROM (
      SELECT CASE n.value WHEN 0 THEN d.h WHEN 1 THEN d.label WHEN 2 THEN lower(trim(d.nm)) WHEN 3 THEN lower(trim(d.nj)) ELSE lower(trim(d.ne)) END AS key,
             CASE n.value WHEN 0 THEN d.deep WHEN 1 THEN 2 + d.deep ELSE 4 END AS rank, d.sid
      FROM (SELECT h, deep, nm, nj, ne, sid,
                   CASE WHEN length(h) - length(replace(h, '.', '')) = 1 THEN substr(h, 1, instr(h, '.') - 1)
                        WHEN length(h) - length(replace(h, '.', '')) = 2 AND substr(substr(h, instr(h, '.') + 1), 1, instr(substr(h, instr(h, '.') + 1), '.') - 1) IN ('co', 'or', 'ne', 'ac', 'go', 'lg', 'ed', 'gr', 'ad', 'com', 'net', 'org', 'gov', 'edu') THEN substr(h, 1, instr(h, '.') - 1) END AS label
            FROM (SELECT CASE WHEN h0 LIKE 'www.%.%' THEN substr(h0, 5) ELSE h0 END AS h, deep, nm, nj, ne, sid
                  FROM (SELECT CASE WHEN instr(hp, ':') > 0 THEN substr(hp, 1, instr(hp, ':') - 1) ELSE hp END AS h0, substr(r, length(hp) + 1) NOT IN ('', '/') AS deep, nm, nj, ne, sid
                        FROM (SELECT lower(substr(r, 1, instr(replace(replace(r, '?', '/'), '#', '/') || '/', '/') - 1)) AS hp, r, nm, nj, ne, sid
                              FROM (SELECT CASE WHEN instr(NEW.url, '://') > 0 THEN substr(NEW.url, instr(NEW.url, '://') + 3) ELSE '' END AS r, NEW.name AS nm, NEW.name_ja AS nj, NEW.name_en AS ne, NEW.id AS sid))))) d
      CROSS JOIN json_each('[0,1,2,3,4]') n
    ) WHERE key IS NOT NULL AND key <> '' GROUP BY sid, key;
END;
-- url・名前・id が実際に変わったときだけ作り直す（同じ値を SET し直す UPDATE では書き込まない）。
DROP TRIGGER IF EXISTS sites_search_keys_au;
CREATE TRIGGER sites_search_keys_au AFTER UPDATE OF id, url, name, name_ja, name_en ON sites
WHEN NEW.id IS NOT OLD.id OR NEW.url IS NOT OLD.url OR NEW.name IS NOT OLD.name OR NEW.name_ja IS NOT OLD.name_ja OR NEW.name_en IS NOT OLD.name_en BEGIN
  DELETE FROM site_search_keys WHERE site_id = OLD.id;
  INSERT OR IGNORE INTO site_search_keys (key, site_id, rank)
    SELECT key, sid, MIN(rank) FROM (
      SELECT CASE n.value WHEN 0 THEN d.h WHEN 1 THEN d.label WHEN 2 THEN lower(trim(d.nm)) WHEN 3 THEN lower(trim(d.nj)) ELSE lower(trim(d.ne)) END AS key,
             CASE n.value WHEN 0 THEN d.deep WHEN 1 THEN 2 + d.deep ELSE 4 END AS rank, d.sid
      FROM (SELECT h, deep, nm, nj, ne, sid,
                   CASE WHEN length(h) - length(replace(h, '.', '')) = 1 THEN substr(h, 1, instr(h, '.') - 1)
                        WHEN length(h) - length(replace(h, '.', '')) = 2 AND substr(substr(h, instr(h, '.') + 1), 1, instr(substr(h, instr(h, '.') + 1), '.') - 1) IN ('co', 'or', 'ne', 'ac', 'go', 'lg', 'ed', 'gr', 'ad', 'com', 'net', 'org', 'gov', 'edu') THEN substr(h, 1, instr(h, '.') - 1) END AS label
            FROM (SELECT CASE WHEN h0 LIKE 'www.%.%' THEN substr(h0, 5) ELSE h0 END AS h, deep, nm, nj, ne, sid
                  FROM (SELECT CASE WHEN instr(hp, ':') > 0 THEN substr(hp, 1, instr(hp, ':') - 1) ELSE hp END AS h0, substr(r, length(hp) + 1) NOT IN ('', '/') AS deep, nm, nj, ne, sid
                        FROM (SELECT lower(substr(r, 1, instr(replace(replace(r, '?', '/'), '#', '/') || '/', '/') - 1)) AS hp, r, nm, nj, ne, sid
                              FROM (SELECT CASE WHEN instr(NEW.url, '://') > 0 THEN substr(NEW.url, instr(NEW.url, '://') + 3) ELSE '' END AS r, NEW.name AS nm, NEW.name_ja AS nj, NEW.name_en AS ne, NEW.id AS sid))))) d
      CROSS JOIN json_each('[0,1,2,3,4]') n
    ) WHERE key IS NOT NULL AND key <> '' GROUP BY sid, key;
END;
DROP TRIGGER IF EXISTS sites_search_keys_ad;
CREATE TRIGGER sites_search_keys_ad AFTER DELETE ON sites BEGIN
  DELETE FROM site_search_keys WHERE site_id = OLD.id;
END;
-- 既存行の埋め直し（上のトリガと同じ式）。
DELETE FROM site_search_keys;
INSERT OR IGNORE INTO site_search_keys (key, site_id, rank)
    SELECT key, sid, MIN(rank) FROM (
      SELECT CASE n.value WHEN 0 THEN d.h WHEN 1 THEN d.label WHEN 2 THEN lower(trim(d.nm)) WHEN 3 THEN lower(trim(d.nj)) ELSE lower(trim(d.ne)) END AS key,
             CASE n.value WHEN 0 THEN d.deep WHEN 1 THEN 2 + d.deep ELSE 4 END AS rank, d.sid
      FROM (SELECT h, deep, nm, nj, ne, sid,
                   CASE WHEN length(h) - length(replace(h, '.', '')) = 1 THEN substr(h, 1, instr(h, '.') - 1)
                        WHEN length(h) - length(replace(h, '.', '')) = 2 AND substr(substr(h, instr(h, '.') + 1), 1, instr(substr(h, instr(h, '.') + 1), '.') - 1) IN ('co', 'or', 'ne', 'ac', 'go', 'lg', 'ed', 'gr', 'ad', 'com', 'net', 'org', 'gov', 'edu') THEN substr(h, 1, instr(h, '.') - 1) END AS label
            FROM (SELECT CASE WHEN h0 LIKE 'www.%.%' THEN substr(h0, 5) ELSE h0 END AS h, deep, nm, nj, ne, sid
                  FROM (SELECT CASE WHEN instr(hp, ':') > 0 THEN substr(hp, 1, instr(hp, ':') - 1) ELSE hp END AS h0, substr(r, length(hp) + 1) NOT IN ('', '/') AS deep, nm, nj, ne, sid
                        FROM (SELECT lower(substr(r, 1, instr(replace(replace(r, '?', '/'), '#', '/') || '/', '/') - 1)) AS hp, r, nm, nj, ne, sid
                              FROM (SELECT CASE WHEN instr(s.url, '://') > 0 THEN substr(s.url, instr(s.url, '://') + 3) ELSE '' END AS r, s.name AS nm, s.name_ja AS nj, s.name_en AS ne, s.id AS sid FROM sites s))))) d
      CROSS JOIN json_each('[0,1,2,3,4]') n
    ) WHERE key IS NOT NULL AND key <> '' GROUP BY sid, key;
