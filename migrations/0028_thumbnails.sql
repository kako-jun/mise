-- サムネイル（#130 第1段）。撮影した画像（R2 の thumb/<hash>.webp）を指す列と、読み込み用の極小プレースホルダ。
-- 撮影の状態（成否・期限・失敗の回数・除外・obscured 等）は D1 に持たず、手元の台帳（var/thumbnails.sqlite。scripts/thumbnail/）に持つ。D1 には表示に要るものだけ。
-- thumb_path:        内容ハッシュ名 <32 桁 16 進>.webp（配信は /thumb/<hash>.webp。favicon_path と同じ形）。撮れていなければ NULL
-- thumb_lqip:        極小プレースホルダ（data:image/webp;base64,...。約 130 文字）。画像が無ければ NULL
-- thumb_captured_at: 撮影日時（ISO 8601 UTC）
-- 追加するだけの後方互換（旧 Worker はこれらの列を読まない）。既存行は NULL のまま。
-- sites の UPDATE トリガ（0018/0020 の search_text、0026 の category_counts、0027 の sites_fts）はいずれも `UPDATE OF <列>` で列を限っており、この 3 列の更新では発火しない（tests/thumb-migration.test.mjs で確認）。
-- ★ 戻し方: ALTER TABLE sites DROP COLUMN thumb_path; thumb_lqip; thumb_captured_at;（Worker が列を読むので、戻す前に旧 Worker へ戻す）。
ALTER TABLE sites ADD COLUMN thumb_path TEXT;
ALTER TABLE sites ADD COLUMN thumb_lqip TEXT;
ALTER TABLE sites ADD COLUMN thumb_captured_at TEXT;
