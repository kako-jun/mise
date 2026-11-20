-- ファビコンもサムネイル（0028）と同じ「1 サイト 1 画像・サイト固定キー・上書き」にそろえる（#163）。
-- favicon_v: 表示のバージョン（内容ハッシュの先頭 8 桁の 16 進など、変更検知に使える短い値。API の favicon_url の ?v= の元）。画像が無い・旧データ（内容ハッシュ名のまま）は NULL。
-- favicon_path: 新しい規則では R2 の icon/ 以下のサイト固定のキー名 <sha256(site_id) の先頭 32 桁>.webp（撮り直しでも変わらない）。旧データは内容ハッシュ名 <hash>.webp のまま。
--   Worker は favicon_v が NULL のサイトでは従来どおり /icon/<favicon_path>、favicon_v があるサイトでは /icon/<favicon_path>?v=<favicon_v> を返す（どちらも同じ /icon/ ルートで引ける）。
-- 追加するだけの後方互換（旧 Worker はこの列を読まない）。既存行は NULL のまま。
-- sites の UPDATE トリガ（0018/0020 の search_text、0026 の category_counts、0027 の sites_fts）はいずれも `UPDATE OF <列>` で列を限っており、favicon_v の更新では発火しない（tests/thumb-migration.test.mjs で確認）。
-- ★ 戻し方: ALTER TABLE sites DROP COLUMN favicon_v;（Worker が列を読むので、戻す前に旧 Worker へ戻す）。
ALTER TABLE sites ADD COLUMN favicon_v TEXT;
