-- 検索の前方一致の段（#301）: site_search_keys を「鍵の長さ → 鍵」の順に引く式インデックス。
-- 前方一致の鍵が多い短い語（1〜2 文字など）で、段の長さの上限 T を決める（短い順に PREFIX_MAX + 1 行目まで引く）・長さ T 以下の鍵だけを読む（length(key) の IN の各値で key の範囲を引く）。
-- 無くても検索は壊れない: 前方一致の鍵が PREFIX_MAX 行以下の語は主キーの範囲で全部読み、超える語だけ前方一致の段を作らない（src/sites.js の prefixT）。
-- 書き込み: site_search_keys の行数ぶんの索引（1 サイトあたり 1〜5 行）。派生物（消すには DROP INDEX idx_site_search_keys_len）。
CREATE INDEX IF NOT EXISTS idx_site_search_keys_len ON site_search_keys (length(key), key);
