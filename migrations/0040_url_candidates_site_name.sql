-- オンライン補強: LLM が url_candidates に付けるサイト名(<title> が名前として使えない行の補い。#273)。
-- 非公開: 公開 API・SSR は絶対に SELECT しない（url_candidates の他の列と同じ）。掲載段が sites.name に使う候補の 1 つ。
-- 戻し方: ALTER TABLE url_candidates DROP COLUMN site_name;
ALTER TABLE url_candidates ADD COLUMN site_name TEXT;
