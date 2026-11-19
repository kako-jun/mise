-- 検索の全文索引（#126 第3段）。sites.search_text の trigram 索引（FTS5 の外部コンテンツ表）。派生物で、正本は sites.search_text。
-- 3 文字以上の検索語の絞り込みを instr() の全件走査から索引の引き（MATCH）に替える。1〜2 文字は trigram では 0 件を返すので src/sites.js が instr に分岐する。
-- ★ この migration は「空の表とトリガ」を作るだけ。既存行の構築は含めない（10 万件の 'rebuild' は 12〜24 秒かかり D1 の 1 クエリ 30 秒の上限に近い）。
--   構築は `node scripts/build-search-fts.mjs`（1 万行ずつ分けて流す。冪等）。構築が終わるまで fts_state.ready = 0 のままで、検索コードは instr の経路を使い（構築前に MATCH を使うと取りこぼす）、下のトリガは何もしない
--   （まだ索引に入っていない行の 'delete' を渡すと外部コンテンツ表の索引が壊れる = 構築前に既存のサイトを削除・更新できなくなるため）。
-- ★ 仮想表があると `wrangler d1 export` は使えない（docs/operations/deploy.md のバックアップは Time Travel の bookmark が主）。
-- ★ 戻し方: DROP TRIGGER sites_fts_ai / sites_fts_ad / sites_fts_au; DROP TABLE sites_fts; DROP TABLE fts_state; （検索コードは表が無いと instr に落ちる）。
CREATE VIRTUAL TABLE IF NOT EXISTS sites_fts USING fts5(search_text, content='sites', content_rowid='rowid', tokenize='trigram');
-- 索引が構築済みで検索に使ってよいか（0 = 未構築・構築中。build-search-fts.mjs が構築の前に 0、検証の後に 1 にする）。
CREATE TABLE IF NOT EXISTS fts_state (name TEXT PRIMARY KEY, ready INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
INSERT OR IGNORE INTO fts_state (name, ready) VALUES ('sites_fts', 0);
-- 同期: sites の登録・削除・search_text の変更（ready = 1 のときだけ）。外部コンテンツ表の削除と更新は、古い値を 'delete' で渡して索引から外す。
-- 0018・0020 のトリガが search_text を書き直す（入れ子で UPDATE OF search_text が発火する）ので、タグの追加・変更・削除も反映される。
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
