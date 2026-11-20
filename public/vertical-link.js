// 通常画面 → 縦フィード(?view=feed)の入口リンクの href を作る純関数 (#178)。app.js（ブラウザ）と src/seo.js（SSR）が共有する。DOM には触れない。
// 縦フィード(feed.js)が URL から受け取るのは scope（パスの /jp/ /jp/13/ か ?scope=）・cat（トップ階層のカテゴリ id 1 つ）・sort・seed だけ。
// 通常画面の状態のうち、引き継げるのは範囲(scope)とトップ階層のカテゴリ(cat)。引き継げず捨てるのは、2 階層目以降のカテゴリ・検索語(q)・タグ(tag)（フィード側に絞り込みが無いため。黙って落とすのでなく docs/design/ui.md に明記）。
(g => {
  const SCOPE_RE = /^(all|[a-z]{2}(-[a-z0-9]{1,3})?)$/;
  const CAT_RE = /^[A-Za-z0-9_-]{1,64}$/;
  // scope: 'all' / 'jp' / 'jp-13'。rootCategory: トップ階層のカテゴリ id（無ければ空）。不正な値は無かったことにする。
  function feedEntryHref(scope, rootCategory) {
    const s = typeof scope === 'string' && SCOPE_RE.test(scope) ? scope : 'jp';
    const q = ['view=feed'];
    if (typeof rootCategory === 'string' && CAT_RE.test(rootCategory)) q.push('cat=' + encodeURIComponent(rootCategory));
    return `/${s.replace('-', '/')}/?${q.join('&')}`;
  }
  g.MiseFeedEntry = { feedEntryHref };
})(globalThis);
