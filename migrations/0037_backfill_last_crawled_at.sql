-- 画面の「最終チェック / Last checked」は sites.last_crawled_at だけを見る(#201)。再巡回(site_checks)に成功の記録があるのに
-- last_crawled_at が空のサイトだけ、その成功日時(last_ok_at)で埋める。失敗しただけの last_checked_at・登録日では埋めない(チェックしていない日付を出さない)。
-- 既に値があるサイトは触らない。冪等。本番適用は人が行う(自動では流さない)。
UPDATE sites SET last_crawled_at = (SELECT c.last_ok_at FROM site_checks c WHERE c.site_id = sites.id AND c.last_ok_at IS NOT NULL)
WHERE last_crawled_at IS NULL AND EXISTS (SELECT 1 FROM site_checks c WHERE c.site_id = sites.id AND c.last_ok_at IS NOT NULL);
