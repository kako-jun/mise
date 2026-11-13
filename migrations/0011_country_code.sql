-- サイトの属する国（運営者の所在国）。ISO 3166-1 alpha-2 大文字2文字。確認できないものは NULL（旗を出さない）。
-- 確認根拠は docs/site-schema.md の「運営者の所在国」を参照。ドメイン（.jp 等）だけでは決めない。
ALTER TABLE sites ADD COLUMN country_code TEXT CHECK (country_code IS NULL OR (length(country_code) = 2 AND country_code GLOB '[A-Z][A-Z]'));
-- sample-1〜4: 作者（kako-jun）自身のサイト（所有者情報であり、サイト上の公開記載による確認ではない）。sample-5〜7: 運営主体が日本の機関・団体（青空文庫・国立国会図書館・国立天文台）。
UPDATE sites SET country_code='JP' WHERE id IN ('sample-1','sample-2','sample-3','sample-4','sample-5','sample-6','sample-7');
-- collected-kofun-map: 運営ページに「都内在住のWebプログラマと事務員のチームで開発・運営」とある。
-- collected-zukan-bouz: トップに「運営： ぼうずコンニャク株式会社」（日本の会社形態）。
-- collected-stellarscenes: 作者紹介ページの居住地が茨城県日立市。
UPDATE sites SET country_code='JP' WHERE id IN ('collected-kofun-map','collected-zukan-bouz','collected-stellarscenes');
