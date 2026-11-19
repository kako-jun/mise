-- 並びの先頭に bulk_import（台帳などからの一括投入は後ろ）を足したので、新しい定義のインデックスを作り、旧定義のものを消す（#71）。
-- 前提: 0015 が bulk_import 列を追加する。src/sites.js の SORT_KEYS と同じ列・式・向き（sortIndexSql() が出力する文字列）。
CREATE INDEX IF NOT EXISTS idx_sites_sort_9b34c1ec ON sites (bulk_import, datetime(COALESCE(source_updated_at, registered_at)) DESC, id);
DROP INDEX IF EXISTS idx_sites_sort_2489145a;
