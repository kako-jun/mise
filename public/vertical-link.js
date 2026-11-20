// 通常画面 → 縦フィード(?view=feed)の入口リンクの href を作る純関数 (#178)。app.js（ブラウザ）と src/seo.js（SSR）が共有する。DOM には触れない。
// 縦フィード(feed.js)が URL から受け取るのは scope（パスの /jp/ /jp/13/ か ?scope=）・cat（トップ階層のカテゴリ id 1 つ）・sort・seed だけ。
// 通常画面の状態のうち、引き継げるのは範囲(scope)とトップ階層のカテゴリ(cat)。引き継げず捨てるのは、2 階層目以降のカテゴリ・検索語(q)・タグ(tag)（フィード側に絞り込みが無いため）。捨てる状態のときは入口の下に NOTE を画面に出す（黙って捨てない）。
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
  // 引き継げない状態（検索語・タグ・2 段目以降のカテゴリのどれかが有効）のときだけ、入口の下に出す 1 行。文言はここ 1 か所（app.js の words と src/seo.js が共有。SSR は英語）。
  const NOTE = { ja: '範囲とトップのカテゴリだけ引き継ぎます', en: 'Only scope and top category carry over' };
  // search: 検索語またはタグの絞り込み中か、depth: カテゴリの階層の深さ。
  const feedEntryLossy = (search, depth) => !!search || depth > 1;
  g.MiseFeedEntry = { feedEntryHref, feedEntryLossy, NOTE };
})(globalThis);
