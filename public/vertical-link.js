// カテゴリ URL を通常画面・縦ビュー・SSR で共有する。DOM には触れない。
(g => {
  const SCOPE_RE = /^(all|[a-z]{2}(-[a-z0-9]{1,3})?)$/;
  const CAT_RE = /^[A-Za-z0-9_-]{1,64}$/;
  const DEFAULT_SCOPE = 'jp', PAGE_SIZE = 40;
  // 新着は掲載時刻から作る仮想フォルダ。分類用のカテゴリには登録しない。
  const RECENT_CATEGORY = 'recent';
  const RECENT_DAYS = 30;
  const recentFolder = { id: RECENT_CATEGORY, parent_id: null, name_ja: '新着', name_en: 'New arrivals' };
  const validScope = value => typeof value === 'string' && SCOPE_RE.test(value);

  function scopeFromPath(pathname) {
    const match = pathname.match(/^\/(?:([a-z]{2})\/([a-z0-9]{1,3})|([a-z]{2}|all))\/$/);
    if (!match) return DEFAULT_SCOPE;
    return match[1] ? `${match[1]}-${match[2]}` : match[3];
  }

  const validPage = page => Number.isInteger(page) && page >= 1 && page <= 999999;
  const parsePage = raw => raw == null || raw === '' ? 1 : /^\d{1,6}$/.test(raw) && validPage(Number(raw)) ? Number(raw) : null;

  function directoryHref(scope, path = [], page = 1) {
    const s = validScope(scope) ? scope : DEFAULT_SCOPE;
    const params = new URLSearchParams();
    for (const id of path) {
      if (!CAT_RE.test(id)) break;
      params.append('category', id);
    }
    if (validPage(page) && page > 1 && params.has('category')) params.set('page', String(page));
    const q = params.toString();
    return `/${s.replace('-', '/')}/${q ? '?' + q : ''}`;
  }

  function feedEntryHref(scope, path = [], page = 1) {
    const url = directoryHref(scope, path, page);
    return url + (url.includes('?') ? '&' : '?') + 'view=feed';
  }

  const sitePositionId = position => `site-row-${position}`;

  function continueListHref(scope, path, page, count) {
    if (!path.length) return directoryHref(scope, path, page);
    const offset = (page - 1) * PAGE_SIZE + count;
    return `${directoryHref(scope, path, Math.floor(offset / PAGE_SIZE) + 1)}#${sitePositionId(offset + 1)}`;
  }

  // 最終チェック日（#201）。一覧・縦フィードで同じ判定を使う。last_crawled_at だけを見て、無い・読めないなら null
  // （登録日 registered_at や元サイトの更新日 source_updated_at で代用しない）。形式は YYYY-MM-DD
  const checkedDate = site => {
    const v = site && site.last_crawled_at;
    if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}/.test(v)) return null;
    const t = Date.parse(v);
    // 現在時刻 + 1 日より先（未来日）は偽の日付なので出さない。日付は保存値（UTC）の先頭 10 桁
    return Number.isNaN(t) || t > Date.now() + 86400000 ? null : v.slice(0, 10);
  };

  g.MiseFeedEntry = { checkedDate, DEFAULT_SCOPE, PAGE_SIZE, parsePage, validScope, scopeFromPath, directoryHref, feedEntryHref, sitePositionId, continueListHref, RECENT_CATEGORY, RECENT_DAYS, recentFolder };
})(globalThis);
