-- カテゴリ件数の集計表とトリガ（#126 第1段）。
-- 目的: 再帰 CTE（src/seo.js の旧 loadCategories）が全サイトを走査するのをやめ、読み取りを「カテゴリ数 × 範囲」の数百行にする。
--   10 万件のとき /api/categories は 82 万行 → 約 500 行、トップ（SSR）は 112 万行 → 約 500 行、sitemap は 1,670 万行 → 約 3,000 行（Issue #126 の実測）。
-- ★ 正本は sites / site_categories / categories。ここで作る 3 表は「派生物」で、トリガが常に追従させる。ずれたら下の「初期集計」と同じ文で作り直せる。
--   意味: category_counts.n = 「そのカテゴリ自身と全子孫のどれかに属するサイトの数（同じサイトが複数の子孫に属しても 1 件）」。旧 loadCategories の
--   再帰 CTE（COUNT(DISTINCT s.id)）と完全に同じ。scope は 'all'（全世界）か country_code（その国のサイトだけ。country_code が NULL のサイトは 'all' のみ）。
--   n = 0 の行は「空」。行が無いカテゴリも読み側が 0 とみなす（COALESCE）。
-- 順序（一回きりで安全）: 表を作る → 閉包表を埋める → 件数表を埋める → トリガを作る。トリガは最後なので、初期集計中は発火せず二重に数えない。
--   この migration は 1 つのトランザクション相当で実行される（D1 の migrations apply）。初期集計は既存サイトの読み取り 1 回（現在約 2,000 件、10 万件でも約 290 万行読み・約 3,000 行書き）。

-- (1) 閉包表: 祖先 → 子孫（自分自身を含む）。categories は 300 行台・深さ 3 なので小さい。
CREATE TABLE category_closure (
  ancestor_id   TEXT NOT NULL REFERENCES categories(id),
  descendant_id TEXT NOT NULL REFERENCES categories(id),
  PRIMARY KEY (descendant_id, ancestor_id)
) WITHOUT ROWID;
CREATE INDEX idx_category_closure_ancestor ON category_closure (ancestor_id, descendant_id);
-- (2) 件数表と範囲件数表。
CREATE TABLE category_counts (
  category_id TEXT NOT NULL REFERENCES categories(id),
  scope       TEXT NOT NULL,   -- 'all' または country_code
  n           INTEGER NOT NULL,
  PRIMARY KEY (category_id, scope)
) WITHOUT ROWID;
CREATE TABLE scope_counts (   -- 範囲ごとのサイト数（loadScopes・activity の total・既定一覧の total）
  scope TEXT PRIMARY KEY,     -- 'all' または country_code
  n     INTEGER NOT NULL
) WITHOUT ROWID;

-- (3) 初期集計（既存の再帰 CTE と同じ意味）。
INSERT INTO category_closure (ancestor_id, descendant_id)
  WITH RECURSIVE t(a, d) AS (SELECT id, id FROM categories UNION SELECT t.a, c.id FROM t JOIN categories c ON c.parent_id = t.d)
  SELECT a, d FROM t;
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id GROUP BY cl.ancestor_id;
INSERT INTO category_counts (category_id, scope, n)
  SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
  JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id WHERE s.country_code IS NOT NULL GROUP BY cl.ancestor_id, s.country_code;
INSERT INTO scope_counts (scope, n) SELECT 'all', COUNT(*) FROM sites;
INSERT INTO scope_counts (scope, n) SELECT country_code, COUNT(*) FROM sites WHERE country_code IS NOT NULL GROUP BY country_code;

-- (4) トリガ。書き込み量の見積り（1 サイトの投入 = sites 1 行 + 所属カテゴリ k 個。深さ 3 のカテゴリ木で、祖先は自分を含め最大 3）:
--   sites INSERT: scope_counts 最大 2 行（'all' と国）。
--   site_categories INSERT: 祖先（最大 3）× 範囲（最大 2）= 最大 6 行。同じサイトが同じ祖先の下の別の子孫に既に属していれば、その祖先は増やさない。
--   → 通常（国あり・カテゴリ 1〜3 個）は 1 サイトあたり +3〜+14 行程度（実測: 夜間投入で +8.4 行/サイト、--update-categories で +7.3 行/サイト。単体の登録は 21 行。Issue の「+6 行/サイト」は一括 5,000 件 = 65,000 → 95,000 行の平均）。
--   DELETE も同数。UPDATE の国変更は祖先（最大 3 × k）× 2。
-- 同じサイトが同じ祖先の下の複数の子孫に属するときの二重カウント回避: INSERT は「そのサイトの、祖先の部分木に入る所属行がちょうど 1 件（今入れた行だけ）」の祖先だけ +1、
--   DELETE は「消した後に 0 件」の祖先だけ -1 にする。部分木の判定は閉包表の索引 (descendant_id, ancestor_id) で引く。
-- 存在しないサイトを指す行（FK 違反の孤児）は旧クエリ（sites と JOIN）が数えなかったので、トリガも sites に行があるときだけ数える。

-- categories の追加・親の変更・削除に閉包表を追従させる（カテゴリは migration でしか変えないが、変えても件数が壊れないようにする）。
CREATE TRIGGER category_closure_ai AFTER INSERT ON categories BEGIN
  INSERT INTO category_closure (ancestor_id, descendant_id) SELECT ancestor_id, NEW.id FROM category_closure WHERE descendant_id = NEW.parent_id;
  INSERT INTO category_closure (ancestor_id, descendant_id) VALUES (NEW.id, NEW.id);
END;
-- 親の変更: 部分木（NEW.id とその子孫）の「外側の祖先」への行を消し、新しい親の祖先と部分木の全組み合わせを足す。
--   その後、旧親・新親の祖先（自分を含む）の件数だけを再計算する（部分木の中の件数は変わらない）。再計算は部分木の所属行を読むので重いが、親の変更は migration でしか起きない。
CREATE TRIGGER category_closure_au AFTER UPDATE OF parent_id ON categories WHEN OLD.parent_id IS NOT NEW.parent_id BEGIN
  DELETE FROM category_closure
   WHERE descendant_id IN (SELECT descendant_id FROM category_closure WHERE ancestor_id = NEW.id)
     AND ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id = NEW.id AND ancestor_id <> NEW.id);
  INSERT INTO category_closure (ancestor_id, descendant_id)
    SELECT p.ancestor_id, d.descendant_id FROM category_closure p, category_closure d WHERE p.descendant_id = NEW.parent_id AND d.ancestor_id = NEW.id;
  DELETE FROM category_counts WHERE category_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id));
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id)) GROUP BY cl.ancestor_id;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.country_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.parent_id, NEW.parent_id)) GROUP BY cl.ancestor_id, s.country_code;
END;
-- 削除（葉だけ。所属行は FK で先に消えていて、その DELETE トリガが祖先の件数を減らし終えている）。
CREATE TRIGGER category_closure_ad AFTER DELETE ON categories BEGIN
  DELETE FROM category_closure WHERE descendant_id = OLD.id OR ancestor_id = OLD.id;
  DELETE FROM category_counts WHERE category_id = OLD.id;
END;

-- サイトの所属カテゴリの追加。
CREATE TRIGGER category_counts_sc_ai AFTER INSERT ON site_categories BEGIN
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, sc.scope, 1
    FROM category_closure cl,
         (SELECT 'all' AS scope FROM sites WHERE id = NEW.site_id UNION ALL SELECT country_code FROM sites WHERE id = NEW.site_id AND country_code IS NOT NULL) sc
    WHERE cl.descendant_id = NEW.category_id
      AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = NEW.site_id) = 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
-- 所属カテゴリの削除（INSERT OR IGNORE で無視された行・無い行ではトリガは発火しない）。
CREATE TRIGGER category_counts_sc_ad AFTER DELETE ON site_categories BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN (SELECT 'all' FROM sites WHERE id = OLD.site_id UNION ALL SELECT country_code FROM sites WHERE id = OLD.site_id AND country_code IS NOT NULL)
     AND category_id IN (SELECT cl.ancestor_id FROM category_closure cl WHERE cl.descendant_id = OLD.category_id
          AND (SELECT COUNT(*) FROM site_categories x JOIN category_closure c2 ON c2.descendant_id = x.category_id AND c2.ancestor_id = cl.ancestor_id WHERE x.site_id = OLD.site_id) = 0);
END;
-- 所属行そのものの書き換え（site_id / category_id の UPDATE。通常のコードは INSERT / DELETE しか使わない）: 旧・新の祖先の件数を再計算する。
CREATE TRIGGER category_counts_sc_au AFTER UPDATE ON site_categories BEGIN
  DELETE FROM category_counts WHERE category_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id));
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, 'all', COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id)) GROUP BY cl.ancestor_id;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT cl.ancestor_id, s.country_code, COUNT(DISTINCT sc.site_id) FROM category_closure cl
    JOIN site_categories sc ON sc.category_id = cl.descendant_id JOIN sites s ON s.id = sc.site_id
    WHERE s.country_code IS NOT NULL AND cl.ancestor_id IN (SELECT ancestor_id FROM category_closure WHERE descendant_id IN (OLD.category_id, NEW.category_id)) GROUP BY cl.ancestor_id, s.country_code;
END;

-- サイト自身。INSERT: 範囲件数を増やす。通常は所属行がまだ無いが、先に所属行が入っていた場合（FK を後で満たす順序）に備えて、その分の件数も足す。
CREATE TRIGGER category_counts_sites_ai AFTER INSERT ON sites BEGIN
  INSERT INTO scope_counts (scope, n) SELECT 'all', 1 UNION ALL SELECT NEW.country_code, 1 WHERE NEW.country_code IS NOT NULL
    ON CONFLICT (scope) DO UPDATE SET n = n + 1;
  INSERT INTO category_counts (category_id, scope, n)
    SELECT a.ancestor_id, sc.scope, 1
    FROM (SELECT DISTINCT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id) a,
         (SELECT 'all' AS scope UNION ALL SELECT NEW.country_code WHERE NEW.country_code IS NOT NULL) sc
    WHERE 1
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
END;
-- DELETE は BEFORE: 所属行が残っているうちに（通常は先に消してあり空振り）、残りの分の件数を引く。その後に消える所属行はサイトが無いのでトリガは何も数えない。
CREATE TRIGGER category_counts_sites_bd BEFORE DELETE ON sites BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope IN ('all', OLD.country_code)
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = OLD.id);
  UPDATE scope_counts SET n = n - 1 WHERE scope IN ('all', OLD.country_code);
END;
-- 国の変更: 旧国から新国へ、そのサイトが属する祖先（重複なし）の件数と範囲件数を移す。
CREATE TRIGGER category_counts_sites_au AFTER UPDATE OF country_code ON sites WHEN OLD.country_code IS NOT NEW.country_code BEGIN
  UPDATE category_counts SET n = n - 1
   WHERE scope = OLD.country_code
     AND category_id IN (SELECT cl.ancestor_id FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id WHERE x.site_id = NEW.id);
  INSERT INTO category_counts (category_id, scope, n)
    SELECT DISTINCT cl.ancestor_id, NEW.country_code, 1 FROM site_categories x JOIN category_closure cl ON cl.descendant_id = x.category_id
    WHERE x.site_id = NEW.id AND NEW.country_code IS NOT NULL
    ON CONFLICT (category_id, scope) DO UPDATE SET n = n + 1;
  UPDATE scope_counts SET n = n - 1 WHERE scope = OLD.country_code;
  INSERT INTO scope_counts (scope, n) SELECT NEW.country_code, 1 WHERE NEW.country_code IS NOT NULL ON CONFLICT (scope) DO UPDATE SET n = n + 1;
END;
